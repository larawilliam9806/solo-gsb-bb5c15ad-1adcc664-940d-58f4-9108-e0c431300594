"""核心审计逻辑：逐航段做平面求交，再与时段、高度窗口在“航段分数”域内求交集。

关键设计：航段上任意一点可用分数 f ∈ [0,1] 线性插值（位置、时间、高度都是 f 的线性函数），
因此“平面命中 ∧ 时段命中 ∧ 高度命中”可转化为三个 f 区间的交集：
  - 平面：Shapely 求线段与多边形的交，得到 f 区间（进入）或 f 点（擦边）；
  - 时段：解 t0 + f·Δt ∈ [active_from, active_to]；
  - 高度：解 alt0 + f·Δalt ∈ [min_alt, max_alt]。
三者交集非空即违规；交集退化为点（或仅沿边界滑行）记为 graze，否则为 entry。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from shapely.geometry import LineString, Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity

from .models import Position, Violation, Waypoint, Zone

EPS = 1e-9
# 分数域上判定“退化为点”的容差
POINT_TOL = 1e-7


class TrackOrderError(ValueError):
    """航点未按时间严格递增。"""


class ZoneGeometryError(ValueError):
    """禁飞区几何非法。"""


# ---------------------------------------------------------------- 基础工具

def _epoch(dt: datetime) -> float:
    return dt.timestamp()


def _fraction_window(v0: float, v1: float, lo: float, hi: float) -> Optional[Tuple[float, float]]:
    """求 f ∈ [0,1] 使 lo <= v0 + f·(v1-v0) <= hi，无解返回 None。"""
    if abs(v1 - v0) < EPS:
        return (0.0, 1.0) if lo <= v0 <= hi else None
    f_lo = (lo - v0) / (v1 - v0)
    f_hi = (hi - v0) / (v1 - v0)
    a, b = min(f_lo, f_hi), max(f_lo, f_hi)
    a = max(a, 0.0)
    b = min(b, 1.0)
    if a > b:
        if a - b <= POINT_TOL:  # 恰好相切于一个分数点
            m = (a + b) / 2.0
            return (m, m)
        return None
    return (a, b)


def _intersect_windows(*wins: Tuple[float, float]) -> Optional[Tuple[float, float]]:
    a = max(w[0] for w in wins)
    b = min(w[1] for w in wins)
    if a > b:
        if a - b <= POINT_TOL:
            m = (a + b) / 2.0
            return (m, m)
        return None
    return (a, b)


# ---------------------------------------------------------------- 平面求交

def _planar_hits(line: LineString, poly: BaseGeometry) -> Tuple[List[Dict[str, Any]], List[float]]:
    """线段与多边形的平面关系。

    返回 (spans, touches)：
      spans  = [{"fa":.., "fb":.., "interior":bool}]  进入/贴合边界的连续区间
      touches = [f, ...]                              孤立的切点（如掠过顶点）
    """
    spans: List[Dict[str, Any]] = []
    touches: List[float] = []

    # 退化航段（起止坐标相同）：按点处理
    if line.length < EPS:
        pt = line.interpolate(0.0)
        if poly.contains(pt):
            spans.append({"fa": 0.0, "fb": 1.0, "interior": True})
        elif poly.covers(pt):
            touches.append(0.0)
        return spans, touches

    inter = line.intersection(poly)
    length = line.length

    def frac_of(p: Point) -> float:
        return min(max(line.project(p) / length, 0.0), 1.0)

    def visit(g: BaseGeometry) -> None:
        if g.is_empty:
            return
        gt = g.geom_type
        if gt == "Point":
            touches.append(frac_of(g))
        elif gt == "LineString":
            coords = list(g.coords)
            fa = frac_of(Point(coords[0]))
            fb = frac_of(Point(coords[-1]))
            a, b = min(fa, fb), max(fa, fb)
            if b - a < POINT_TOL:
                touches.append((a + b) / 2.0)
            else:
                # 区间中点落在内部才算真正进入；否则是沿边界滑行（擦边）
                mid = line.interpolate(((a + b) / 2.0) * length)
                spans.append({"fa": a, "fb": b, "interior": bool(poly.contains(mid))})
        else:  # MultiPoint / MultiLineString / GeometryCollection
            for sub in getattr(g, "geoms", []):
                visit(sub)

    visit(inter)
    return spans, touches


# ---------------------------------------------------------------- 航段 × 区域

def _segment_hits(
    seg_idx: int,
    p0: Waypoint,
    p1: Waypoint,
    poly: BaseGeometry,
    z_from: float,
    z_to: float,
    z_min: float,
    z_max: float,
) -> List[Dict[str, Any]]:
    """单条航段与单个禁飞区的命中（f 域区间，已叠加时段与高度窗口）。"""
    line = LineString([(p0.lon, p0.lat), (p1.lon, p1.lat)])
    spans, touches = _planar_hits(line, poly)
    if not spans and not touches:
        return []

    t_win = _fraction_window(_epoch(p0.time), _epoch(p1.time), z_from, z_to)
    if t_win is None:
        return []
    a_win = _fraction_window(p0.alt, p1.alt, z_min, z_max)
    if a_win is None:
        return []

    hits: List[Dict[str, Any]] = []
    for span in spans:
        combined = _intersect_windows((span["fa"], span["fb"]), t_win, a_win)
        if combined is None:
            continue
        fa, fb = combined
        graze = (not span["interior"]) or (fb - fa < POINT_TOL)
        hits.append({"seg": seg_idx, "fa": fa, "fb": fb, "graze": graze})
    for f in touches:
        if t_win[0] - POINT_TOL <= f <= t_win[1] + POINT_TOL and a_win[0] - POINT_TOL <= f <= a_win[1] + POINT_TOL:
            hits.append({"seg": seg_idx, "fa": f, "fb": f, "graze": True})
    return hits


def _merge_hits(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把跨航段首尾相接（共享航点）的命中合并为一条连续违规区间。"""
    merged: List[Dict[str, Any]] = []
    for h in sorted(hits, key=lambda x: (x["seg"], x["fa"])):
        if merged:
            last = merged[-1]
            contiguous = (
                (h["seg"] == last["seg_to"] + 1 and h["fa"] < POINT_TOL and last["fb"] > 1.0 - POINT_TOL)
                or (h["seg"] == last["seg_to"] and h["fa"] - last["fb"] < POINT_TOL)
            )
            if contiguous:
                last["fb"] = h["fb"]
                last["seg_to"] = h["seg"]
                last["graze"] = last["graze"] and h["graze"]
                continue
        merged.append({"seg_from": h["seg"], "seg_to": h["seg"],
                       "fa": h["fa"], "fb": h["fb"], "graze": h["graze"]})
    return merged


# ---------------------------------------------------------------- 结果构造

def _interp_position(p0: Waypoint, p1: Waypoint, f: float) -> Position:
    return Position(
        lat=p0.lat + f * (p1.lat - p0.lat),
        lon=p0.lon + f * (p1.lon - p0.lon),
        alt=p0.alt + f * (p1.alt - p0.alt),
    )


def _interp_time(t0: datetime, t1: datetime, f: float) -> datetime:
    return datetime.fromtimestamp(_epoch(t0) + f * (_epoch(t1) - _epoch(t0)), tz=timezone.utc)


def _build_violation(zone_id: str, rec: Dict[str, Any], waypoints: List[Waypoint]) -> Violation:
    w_in0, w_in1 = waypoints[rec["seg_from"]], waypoints[rec["seg_from"] + 1]
    w_out0, w_out1 = waypoints[rec["seg_to"]], waypoints[rec["seg_to"] + 1]
    return Violation(
        zone_id=zone_id,
        kind="graze" if rec["graze"] else "entry",
        entry_time=_interp_time(w_in0.time, w_in1.time, rec["fa"]),
        exit_time=_interp_time(w_out0.time, w_out1.time, rec["fb"]),
        entry_position=_interp_position(w_in0, w_in1, rec["fa"]),
        exit_position=_interp_position(w_out0, w_out1, rec["fb"]),
        segment_start=rec["seg_from"],
        segment_end=rec["seg_to"],
    )


# ---------------------------------------------------------------- 入口

def _parse_zone_geometry(zone: Zone) -> BaseGeometry:
    try:
        geom = shape(zone.geometry)
    except Exception as exc:
        raise ZoneGeometryError(f"区域 '{zone.id}': 无法解析的 GeoJSON 几何: {exc}") from exc
    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        raise ZoneGeometryError(
            f"区域 '{zone.id}': 几何类型必须为 Polygon 或 MultiPolygon，实际为 {geom.geom_type}"
        )
    if geom.is_empty or geom.area <= 0:
        raise ZoneGeometryError(f"区域 '{zone.id}': 多边形面积为零")
    if not geom.is_valid:
        raise ZoneGeometryError(f"区域 '{zone.id}': 多边形无效（{explain_validity(geom)}）")
    return geom


def audit_track(waypoints: List[Waypoint], zones: List[Zone]) -> List[Violation]:
    # 航点必须严格按时间递增（等时间会让插值失去定义）
    for i in range(1, len(waypoints)):
        if waypoints[i].time <= waypoints[i - 1].time:
            raise TrackOrderError(
                f"航点未按时间严格递增：waypoints[{i}].time={waypoints[i].time.isoformat()} "
                f"<= waypoints[{i - 1}].time={waypoints[i - 1].time.isoformat()}"
            )

    prepared = [(z, _parse_zone_geometry(z)) for z in zones]

    violations: List[Violation] = []
    for zone, geom in prepared:
        z_from, z_to = _epoch(zone.active_from), _epoch(zone.active_to)
        z_min = zone.min_alt if zone.min_alt is not None else float("-inf")
        z_max = zone.max_alt if zone.max_alt is not None else float("inf")

        hits: List[Dict[str, Any]] = []
        for i in range(len(waypoints) - 1):
            hits.extend(_segment_hits(i, waypoints[i], waypoints[i + 1], geom, z_from, z_to, z_min, z_max))
        for rec in _merge_hits(hits):
            violations.append(_build_violation(zone.id, rec, waypoints))

    violations.sort(key=lambda v: (v.entry_time, v.zone_id))
    return violations
