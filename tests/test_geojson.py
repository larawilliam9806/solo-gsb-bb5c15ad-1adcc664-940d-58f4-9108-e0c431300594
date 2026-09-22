"""POST /audit/geojson 的端到端测试。

与 test_audit.py 使用同一禁飞区：平面 [0,10]x[0,10]，时段 10:00-11:00，高度 0-100。
GeoJSON 坐标一律为 [经度, 纬度, 高度]。
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


def geojson(waypoints, zones=None):
    payload = {"waypoints": waypoints, "zones": zones if zones is not None else [copy.deepcopy(ZONE)]}
    return client.post("/audit/geojson", json=payload)


def audit(waypoints, zones=None):
    payload = {"waypoints": waypoints, "zones": zones if zones is not None else [copy.deepcopy(ZONE)]}
    return client.post("/audit", json=payload)


# ---------------------------------------------------------------- 几何形状

def test_entry_single_segment_linestring():
    """单航段横穿：LineString 只有进入点、离开点两个坐标。"""
    r = geojson([wp(-10, 5, 50, "2026-09-22T10:00:00Z"),
                 wp(20, 5, 50, "2026-09-22T10:30:00Z")])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) == 1
    f = body["features"][0]
    assert f["type"] == "Feature"
    assert f["geometry"]["type"] == "LineString"
    coords = f["geometry"]["coordinates"]
    assert len(coords) == 2
    assert coords[0] == [0.0, 5.0, 50.0]
    assert coords[1] == [10.0, 5.0, 50.0]


def test_entry_linestring_contains_intermediate_vertices():
    """跨航段滞留：LineString 必须包含区间内途经的所有航迹顶点。"""
    r = geojson([wp(-10, 5, 50, "2026-09-22T10:00:00Z"),
                 wp(5, 5, 80, "2026-09-22T10:15:00Z"),
                 wp(20, 5, 50, "2026-09-22T10:30:00Z")])
    assert r.status_code == 200, r.text
    f = r.json()["features"][0]
    assert f["geometry"]["type"] == "LineString"
    coords = f["geometry"]["coordinates"]
    assert len(coords) == 3
    # 进入点 ≈ (0, 5, 70)，途经航点 (5, 5, 80)，离开点 = (10, 5, 70)
    assert abs(coords[0][0]) < 1e-9 and coords[0][1] == 5.0
    assert abs(coords[0][2] - 70.0) < 1e-9
    assert coords[1] == [5.0, 5.0, 80.0]
    assert coords[2] == [10.0, 5.0, 70.0]


def test_graze_tangent_point():
    """掠过顶点：进入点 == 离开点，几何为 Point。"""
    r = geojson([wp(-10, 10, 50, "2026-09-22T10:00:00Z"),
                 wp(10, -10, 50, "2026-09-22T10:30:00Z")])
    assert r.status_code == 200, r.text
    f = r.json()["features"][0]
    assert f["properties"]["kind"] == "graze"
    assert f["geometry"]["type"] == "Point"
    lon, lat, alt = f["geometry"]["coordinates"]
    assert abs(lon) < 1e-9 and abs(lat) < 1e-9 and alt == 50.0


def test_graze_along_boundary_linestring():
    """沿边界滑行：进入点 != 离开点，几何为两点 LineString。"""
    r = geojson([wp(-10, 10, 50, "2026-09-22T10:00:00Z"),
                 wp(20, 10, 50, "2026-09-22T10:30:00Z")])
    assert r.status_code == 200, r.text
    f = r.json()["features"][0]
    assert f["properties"]["kind"] == "graze"
    assert f["geometry"]["type"] == "LineString"
    coords = f["geometry"]["coordinates"]
    assert len(coords) == 2
    assert coords[0] == [0.0, 10.0, 50.0]
    assert coords[1] == [10.0, 10.0, 50.0]


# ---------------------------------------------------------------- 属性与顺序

def test_feature_properties():
    r = geojson([wp(-10, 5, 50, "2026-09-22T10:00:00Z"),
                 wp(5, 5, 50, "2026-09-22T10:15:00Z"),
                 wp(20, 5, 50, "2026-09-22T10:30:00Z")])
    assert r.status_code == 200, r.text
    props = r.json()["features"][0]["properties"]
    assert props == {
        "zone_id": "nfz-1",
        "kind": "entry",
        "entry_time": "2026-09-22T10:10:00Z",
        "exit_time": "2026-09-22T10:20:00Z",
        "segment_start": 0,
        "segment_end": 1,
    }


def test_feature_order_matches_audit_response():
    """Feature 顺序与 /audit 的 violations 顺序一致。"""
    zone2 = copy.deepcopy(ZONE)
    zone2["id"] = "nfz-2"
    zone2["geometry"] = {
        "type": "Polygon",
        "coordinates": [[[20, 0], [30, 0], [30, 10], [20, 10], [20, 0]]],
    }
    waypoints = [wp(-10, 5, 50, "2026-09-22T10:00:00Z"),
                 wp(40, 5, 50, "2026-09-22T10:50:00Z")]
    zones = [zone2, copy.deepcopy(ZONE)]  # 故意乱序给区域
    gj = geojson(waypoints, zones)
    au = audit(waypoints, zones)
    assert gj.status_code == 200 and au.status_code == 200
    features = gj.json()["features"]
    violations = au.json()["violations"]
    assert len(features) == len(violations) == 2
    for f, v in zip(features, violations):
        p = f["properties"]
        assert p["zone_id"] == v["zone_id"]
        assert p["kind"] == v["kind"]
        assert p["entry_time"] == v["entry_time"]
        assert p["exit_time"] == v["exit_time"]
        assert p["segment_start"] == v["segment_start"]
        assert p["segment_end"] == v["segment_end"]
    assert [f["properties"]["zone_id"] for f in features] == ["nfz-1", "nfz-2"]


def test_empty_feature_collection():
    r = geojson([wp(0, 0, 0, "2026-09-22T10:00:00Z"),
                 wp(1, 1, 1, "2026-09-22T10:01:00Z")], [])
    assert r.status_code == 200
    assert r.json() == {"type": "FeatureCollection", "features": []}


# ---------------------------------------------------------------- 422 校验

def test_out_of_order_waypoints_422():
    r = geojson([wp(0, 0, 0, "2026-09-22T10:05:00Z"),
                 wp(1, 1, 1, "2026-09-22T10:00:00Z")])
    assert r.status_code == 422
    assert "时间" in r.json()["detail"]


def test_invalid_zone_geometry_422():
    zone = copy.deepcopy(ZONE)
    zone["geometry"] = {  # 蝴蝶结（自相交）
        "type": "Polygon",
        "coordinates": [[[0, 0], [10, 10], [10, 0], [0, 10], [0, 0]]],
    }
    r = geojson([wp(-10, 5, 50, "2026-09-22T10:00:00Z"),
                 wp(20, 5, 50, "2026-09-22T10:30:00Z")], [zone])
    assert r.status_code == 422
    assert "nfz-1" in r.json()["detail"]
