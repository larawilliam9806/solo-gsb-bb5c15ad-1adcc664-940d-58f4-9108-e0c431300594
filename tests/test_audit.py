"""POST /audit 的端到端测试。

测试用统一禁飞区：平面 [0,10]x[0,10]，时段 10:00-11:00，高度 0-100。
"""
from __future__ import annotations

import copy

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

ZONE = {
    "id": "nfz-1",
    "geometry": {
        "type": "Polygon",
        "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
    },
    "active_from": "2026-09-22T10:00:00Z",
    "active_to": "2026-09-22T11:00:00Z",
    "min_alt": 0,
    "max_alt": 100,
}


def wp(lon, lat, alt, time):
    return {"lon": lon, "lat": lat, "alt": alt, "time": time}


def audit(waypoints, zones=None):
    payload = {"waypoints": waypoints, "zones": zones if zones is not None else [copy.deepcopy(ZONE)]}
    return client.post("/audit", json=payload)


# ---------------------------------------------------------------- 正常判定

def test_segment_crossing_with_both_endpoints_outside():
    """两个航点都在区外，但航段横穿——必须能检出（不能只查航点）。"""
    r = audit([wp(-10, 5, 50, "2026-09-22T10:00:00Z"),
               wp(20, 5, 50, "2026-09-22T10:30:00Z")])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["violation_count"] == 1
    v = body["violations"][0]
    assert v["zone_id"] == "nfz-1"
    assert v["kind"] == "entry"
    # 30 分钟飞 30 个经度：lon=0 -> 10:10，lon=10 -> 10:20
    assert v["entry_time"] == "2026-09-22T10:10:00Z"
    assert v["exit_time"] == "2026-09-22T10:20:00Z"
    assert v["entry_position"] == {"lat": 5.0, "lon": 0.0, "alt": 50.0}
    assert v["exit_position"] == {"lat": 5.0, "lon": 10.0, "alt": 50.0}


def test_altitude_interpolation_clips_violation():
    """爬升穿越：高度超过上限的时刻即为违规结束时刻。"""
    r = audit([wp(-10, 5, 0, "2026-09-22T10:00:00Z"),
               wp(20, 5, 200, "2026-09-22T10:30:00Z")])
    assert r.status_code == 200, r.text
    v = r.json()["violations"][0]
    # 平面进入 f=1/3（alt≈66.7），alt 达 100 时 f=0.5 -> 10:15
    assert v["entry_time"] == "2026-09-22T10:10:00Z"
    assert v["exit_time"] == "2026-09-22T10:15:00Z"
    assert abs(v["entry_position"]["alt"] - 200 / 3) < 1e-6
    assert abs(v["exit_position"]["alt"] - 100.0) < 1e-6


def test_above_ceiling_no_violation():
    r = audit([wp(-10, 5, 150, "2026-09-22T10:00:00Z"),
               wp(20, 5, 150, "2026-09-22T10:30:00Z")])
    assert r.status_code == 200
    assert r.json()["violation_count"] == 0


def test_outside_active_window_no_violation():
    zone = copy.deepcopy(ZONE)
    zone["active_from"] = "2026-09-22T10:00:00Z"
    zone["active_to"] = "2026-09-22T10:05:00Z"  # 穿越发生在 10:10-10:20
    r = audit([wp(-10, 5, 50, "2026-09-22T10:00:00Z"),
               wp(20, 5, 50, "2026-09-22T10:30:00Z")], [zone])
    assert r.status_code == 200
    assert r.json()["violation_count"] == 0


def test_time_window_clips_violation():
    """区域 10:15 失效，则违规在 10:15 提前结束（时间插值）。"""
    zone = copy.deepcopy(ZONE)
    zone["active_to"] = "2026-09-22T10:15:00Z"
    r = audit([wp(-10, 5, 50, "2026-09-22T10:00:00Z"),
               wp(20, 5, 50, "2026-09-22T10:30:00Z")], [zone])
    v = r.json()["violations"][0]
    assert v["entry_time"] == "2026-09-22T10:10:00Z"
    assert v["exit_time"] == "2026-09-22T10:15:00Z"
    assert v["exit_position"]["lon"] == 5.0


def test_graze_tangent_at_corner():
    """航迹恰好掠过顶点 (0,0)：记为 graze，进入点=离开点。"""
    r = audit([wp(-10, 10, 50, "2026-09-22T10:00:00Z"),
               wp(10, -10, 50, "2026-09-22T10:30:00Z")])
    assert r.status_code == 200, r.text
    v = r.json()["violations"][0]
    assert v["kind"] == "graze"
    assert v["entry_time"] == v["exit_time"] == "2026-09-22T10:15:00Z"
    assert abs(v["entry_position"]["lon"]) < 1e-9
    assert abs(v["entry_position"]["lat"]) < 1e-9


def test_graze_along_boundary_edge():
    """沿多边形边滑行（lat=10 上边）：全程擦边而非进入。"""
    r = audit([wp(-10, 10, 50, "2026-09-22T10:00:00Z"),
               wp(20, 10, 50, "2026-09-22T10:30:00Z")])
    assert r.status_code == 200, r.text
    v = r.json()["violations"][0]
    assert v["kind"] == "graze"
    assert v["entry_time"] == "2026-09-22T10:10:00Z"
    assert v["exit_time"] == "2026-09-22T10:20:00Z"


def test_multi_segment_stay_inside_merges_to_one_interval():
    """途经区内航点：跨航段的连续滞留应合并为一条违规。"""
    r = audit([wp(-10, 5, 50, "2026-09-22T10:00:00Z"),
               wp(5, 5, 50, "2026-09-22T10:15:00Z"),
               wp(20, 5, 50, "2026-09-22T10:30:00Z")])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["violation_count"] == 1
    v = body["violations"][0]
    assert v["kind"] == "entry"
    assert v["entry_time"] == "2026-09-22T10:10:00Z"
    assert v["exit_time"] == "2026-09-22T10:20:00Z"
    assert v["segment_start"] == 0 and v["segment_end"] == 1


def test_start_inside_zone():
    """起点即在区内：进入位置就是第一个航点。"""
    r = audit([wp(5, 5, 50, "2026-09-22T10:00:00Z"),
               wp(20, 5, 50, "2026-09-22T10:15:00Z")])
    v = r.json()["violations"][0]
    assert v["entry_time"] == "2026-09-22T10:00:00Z"
    assert v["exit_time"] == "2026-09-22T10:05:00Z"
    assert v["entry_position"] == {"lat": 5.0, "lon": 5.0, "alt": 50.0}


def test_violations_sorted_by_time_across_zones():
    zone2 = copy.deepcopy(ZONE)
    zone2["id"] = "nfz-2"
    zone2["geometry"] = {
        "type": "Polygon",
        "coordinates": [[[20, 0], [30, 0], [30, 10], [20, 10], [20, 0]]],
    }
    r = audit([wp(-10, 5, 50, "2026-09-22T10:00:00Z"),
               wp(40, 5, 50, "2026-09-22T10:50:00Z")], [zone2, copy.deepcopy(ZONE)])
    body = r.json()
    assert body["violation_count"] == 2
    times = [v["entry_time"] for v in body["violations"]]
    assert times == sorted(times)
    assert [v["zone_id"] for v in body["violations"]] == ["nfz-1", "nfz-2"]


def test_no_zones_returns_empty():
    r = audit([wp(0, 0, 0, "2026-09-22T10:00:00Z"),
               wp(1, 1, 1, "2026-09-22T10:01:00Z")], [])
    assert r.status_code == 200
    assert r.json() == {"violation_count": 0, "violations": []}


def test_multipolygon_zone():
    zone = copy.deepcopy(ZONE)
    zone["geometry"] = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
            [[[20, 0], [30, 0], [30, 10], [20, 10], [20, 0]]],
        ],
    }
    r = audit([wp(-10, 5, 50, "2026-09-22T10:00:00Z"),
               wp(40, 5, 50, "2026-09-22T10:50:00Z")], [zone])
    assert r.status_code == 200, r.text
    assert r.json()["violation_count"] == 2


# ---------------------------------------------------------------- 422 校验

def test_out_of_order_waypoints_422():
    r = audit([wp(0, 0, 0, "2026-09-22T10:05:00Z"),
               wp(1, 1, 1, "2026-09-22T10:00:00Z")])
    assert r.status_code == 422
    assert "时间" in r.json()["detail"]


def test_equal_timestamps_422():
    r = audit([wp(0, 0, 0, "2026-09-22T10:00:00Z"),
               wp(1, 1, 1, "2026-09-22T10:00:00Z")])
    assert r.status_code == 422


def test_self_intersecting_polygon_422():
    zone = copy.deepcopy(ZONE)
    zone["geometry"] = {  # 蝴蝶结（自相交）
        "type": "Polygon",
        "coordinates": [[[0, 0], [10, 10], [10, 0], [0, 10], [0, 0]]],
    }
    r = audit([wp(-10, 5, 50, "2026-09-22T10:00:00Z"),
               wp(20, 5, 50, "2026-09-22T10:30:00Z")], [zone])
    assert r.status_code == 422
    assert "nfz-1" in r.json()["detail"]


def test_non_polygon_geometry_422():
    zone = copy.deepcopy(ZONE)
    zone["geometry"] = {"type": "Point", "coordinates": [0, 0]}
    r = audit([wp(-10, 5, 50, "2026-09-22T10:00:00Z"),
               wp(20, 5, 50, "2026-09-22T10:30:00Z")], [zone])
    assert r.status_code == 422


def test_bad_time_window_422():
    zone = copy.deepcopy(ZONE)
    zone["active_to"] = zone["active_from"]
    r = audit([wp(-10, 5, 50, "2026-09-22T10:00:00Z"),
               wp(20, 5, 50, "2026-09-22T10:30:00Z")], [zone])
    assert r.status_code == 422


def test_bad_altitude_range_422():
    zone = copy.deepcopy(ZONE)
    zone["min_alt"], zone["max_alt"] = 200, 100
    r = audit([wp(-10, 5, 50, "2026-09-22T10:00:00Z"),
               wp(20, 5, 50, "2026-09-22T10:30:00Z")], [zone])
    assert r.status_code == 422


def test_too_few_waypoints_422():
    r = audit([wp(0, 0, 0, "2026-09-22T10:00:00Z")])
    assert r.status_code == 422
