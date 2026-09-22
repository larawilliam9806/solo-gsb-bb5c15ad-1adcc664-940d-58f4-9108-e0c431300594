"""审计结果 → GeoJSON FeatureCollection 的转换。

几何约定（坐标一律为 [经度, 纬度, 高度]）：
  - entry：LineString，依次为进入点、跨段途经的所有航迹顶点、离开点；
  - graze：进入点与离开点重合时为 Point，否则为进入点 → 离开点的 LineString。
Feature 的顺序与 /audit 响应中 violations 的顺序一致。
"""
from __future__ import annotations

from typing import List

from .models import (
    GeoJsonFeature,
    GeoJsonFeatureCollection,
    Violation,
    ViolationProperties,
    Waypoint,
)


def _coord(lon: float, lat: float, alt: float) -> List[float]:
    return [lon, lat, alt]


def violation_to_feature(violation: Violation, waypoints: List[Waypoint]) -> GeoJsonFeature:
    entry = _coord(violation.entry_position.lon, violation.entry_position.lat,
                   violation.entry_position.alt)
    exit_ = _coord(violation.exit_position.lon, violation.exit_position.lat,
                   violation.exit_position.alt)

    if violation.kind == "entry":
        # 进入点 → 区间跨过的全部航迹顶点（segment_start+1 .. segment_end）→ 离开点
        interior = [
            _coord(w.lon, w.lat, w.alt)
            for w in waypoints[violation.segment_start + 1 : violation.segment_end + 1]
        ]
        geometry = {"type": "LineString", "coordinates": [entry, *interior, exit_]}
    elif entry == exit_:
        geometry = {"type": "Point", "coordinates": entry}
    else:
        geometry = {"type": "LineString", "coordinates": [entry, exit_]}

    return GeoJsonFeature(
        geometry=geometry,
        properties=ViolationProperties(
            zone_id=violation.zone_id,
            kind=violation.kind,
            entry_time=violation.entry_time,
            exit_time=violation.exit_time,
            segment_start=violation.segment_start,
            segment_end=violation.segment_end,
        ),
    )


def violations_to_feature_collection(
    violations: List[Violation], waypoints: List[Waypoint]
) -> GeoJsonFeatureCollection:
    return GeoJsonFeatureCollection(
        features=[violation_to_feature(v, waypoints) for v in violations]
    )
