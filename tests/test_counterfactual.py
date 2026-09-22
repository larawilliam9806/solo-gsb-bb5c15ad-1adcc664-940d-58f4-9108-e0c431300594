"""POST /audit/counterfactual 的端到端测试。

测试用统一禁飞区：平面 [0,10]x[0,10]，时段 10:00-11:00，高度 0-100。
"""
from __future__ import annotations

import copy
import json

from fastapi.testclient import TestClient

from app.counterfactual import counterfactual_audit, shift_waypoints
from app.main import app
from app.models import CounterfactualRequest

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


def counterfactual(waypoints, altitude_offset, zones=None):
    payload = {
        "waypoints": waypoints,
        "zones": zones if zones is not None else [copy.deepcopy(ZONE)],
        "altitude_offset": altitude_offset,
    }
    return client.post("/audit/counterfactual", json=payload)


def crossing_track(alt=50):
    """10:00-10:30 匀速横穿禁飞区的平飞航迹（平面命中 10:10-10:20）。"""
    return [wp(-10, 5, alt, "2026-09-22T10:00:00Z"),
            wp(20, 5, alt, "2026-09-22T10:30:00Z")]


# ---------------------------------------------------------------- 消除 / 引入

def test_raise_above_ceiling_clears_violation():
    """50m 平飞违规；整体抬高 100m 后飞到 150m，超出上限，违规被消除。"""
    r = counterfactual(crossing_track(50), 100)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["altitude_offset"] == 100.0
    assert body["baseline"]["violation_count"] == 1
    assert body["shifted"]["violation_count"] == 0
    assert body["cleared_count"] == 1 and body["introduced_count"] == 0
    v = body["cleared"][0]
    assert v["zone_id"] == "nfz-1"
    assert v["entry_time"] == "2026-09-22T10:10:00Z"
    assert v["exit_time"] == "2026-09-22T10:20:00Z"
    assert body["introduced"] == []


def test_lower_into_altitude_band_introduces_violation():
    """150m 平飞本不违规；整体降低 100m 后进入 0-100m 高度带，引入新违规。"""
    r = counterfactual(crossing_track(150), -100)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["baseline"]["violation_count"] == 0
    assert body["shifted"]["violation_count"] == 1
    assert body["cleared_count"] == 0 and body["introduced_count"] == 1
    v = body["introduced"][0]
    assert v["entry_time"] == "2026-09-22T10:10:00Z"
    assert v["exit_time"] == "2026-09-22T10:20:00Z"
    # 平移后的违规位置高度应为 150 - 100 = 50
    assert v["entry_position"]["alt"] == 50.0
    assert v["exit_position"]["alt"] == 50.0


def test_unrestricted_altitude_violation_survives_shift():
    """区域不限高度时，平移不改变违规：cleared/introduced 均为空。"""
    zone = copy.deepcopy(ZONE)
    del zone["min_alt"], zone["max_alt"]
    r = counterfactual(crossing_track(50), 30, [zone])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["baseline"]["violation_count"] == 1
    assert body["shifted"]["violation_count"] == 1
    assert body["cleared"] == [] and body["introduced"] == []
    # 时刻不变，但 shifted 的命中高度整体抬高 30m
    base_v = body["baseline"]["violations"][0]
    shift_v = body["shifted"]["violations"][0]
    assert base_v["entry_time"] == shift_v["entry_time"] == "2026-09-22T10:10:00Z"
    assert base_v["entry_position"]["alt"] == 50.0
    assert shift_v["entry_position"]["alt"] == 80.0


def test_shifted_time_window_counts_as_cleared_plus_introduced():
    """爬升航迹的违规离区时刻由高度上限决定；平移后时刻改变，
    按匹配键语义呈现为一条 cleared + 一条 introduced。"""
    track = [wp(-10, 5, 0, "2026-09-22T10:00:00Z"),
             wp(20, 5, 200, "2026-09-22T10:30:00Z")]
    r = counterfactual(track, -50)
    assert r.status_code == 200, r.text
    body = r.json()
    # 基线：10:10 进入，10:15 高度越限结束；平移 -50 后 10:20 才离开平面区域
    assert body["cleared_count"] == 1 and body["introduced_count"] == 1
    cleared_v = body["cleared"][0]
    introduced_v = body["introduced"][0]
    assert cleared_v["entry_time"] == introduced_v["entry_time"] == "2026-09-22T10:10:00Z"
    assert cleared_v["exit_time"] == "2026-09-22T10:15:00Z"
    assert introduced_v["exit_time"] == "2026-09-22T10:20:00Z"


def test_zero_offset_no_diff():
    r = counterfactual(crossing_track(50), 0)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["baseline"] == body["shifted"]
    assert body["cleared"] == [] and body["introduced"] == []
    assert body["cleared_count"] == 0 and body["introduced_count"] == 0


# ---------------------------------------------------------------- 稳定性与一致性

def test_baseline_matches_audit_endpoint():
    """counterfactual 的 baseline 必须与 /audit 对同一请求体的结果完全一致。"""
    payload = {"waypoints": crossing_track(50), "zones": [copy.deepcopy(ZONE)]}
    r_cf = client.post("/audit/counterfactual", json={**payload, "altitude_offset": 25})
    r_audit = client.post("/audit", json=payload)
    assert r_cf.status_code == 200 and r_audit.status_code == 200
    assert r_cf.json()["baseline"] == r_audit.json()


def test_same_input_deterministic_output():
    """相同输入两次请求，响应必须逐字节一致（稳定排序）。"""
    track = [wp(-10, 5, 0, "2026-09-22T10:00:00Z"),
             wp(20, 5, 200, "2026-09-22T10:30:00Z")]
    r1 = counterfactual(track, -50)
    r2 = counterfactual(track, -50)
    assert r1.status_code == r2.status_code == 200
    assert r1.content == r2.content


def test_cleared_sorted_by_entry_time_across_zones():
    """多个区域同时被消除时，cleared 按进入时间升序稳定排列。"""
    zone2 = copy.deepcopy(ZONE)
    zone2["id"] = "nfz-2"
    zone2["geometry"] = {
        "type": "Polygon",
        "coordinates": [[[20, 0], [30, 0], [30, 10], [20, 10], [20, 0]]],
    }
    track = [wp(-10, 5, 50, "2026-09-22T10:00:00Z"),
             wp(40, 5, 50, "2026-09-22T10:50:00Z")]
    r = counterfactual(track, 100, [zone2, copy.deepcopy(ZONE)])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cleared_count"] == 2
    times = [v["entry_time"] for v in body["cleared"]]
    assert times == sorted(times)
    assert [v["zone_id"] for v in body["cleared"]] == ["nfz-1", "nfz-2"]


def test_request_object_not_mutated():
    """平移不得修改原请求对象：审计前后航点高度保持不变。"""
    req = CounterfactualRequest(
        waypoints=crossing_track(50),
        zones=[copy.deepcopy(ZONE)],
        altitude_offset=500,
    )
    before = [w.model_copy() for w in req.waypoints]
    counterfactual_audit(req.waypoints, req.zones, req.altitude_offset)
    assert req.waypoints == before


def test_shift_waypoints_returns_new_objects():
    req = CounterfactualRequest(
        waypoints=crossing_track(50),
        zones=[copy.deepcopy(ZONE)],
        altitude_offset=10,
    )
    shifted = shift_waypoints(req.waypoints, 10)
    assert [w.alt for w in shifted] == [60.0, 60.0]
    assert [w.alt for w in req.waypoints] == [50.0, 50.0]
    assert all(s is not o for s, o in zip(shifted, req.waypoints))


# ---------------------------------------------------------------- 422 校验

def test_missing_altitude_offset_422():
    payload = {"waypoints": crossing_track(50), "zones": [copy.deepcopy(ZONE)]}
    r = client.post("/audit/counterfactual", json=payload)
    assert r.status_code == 422


def test_non_finite_altitude_offset_422():
    """非有限数值（NaN/Infinity）的平移量必须被拒绝。"""
    zones = json.dumps([ZONE])
    waypoints = json.dumps(crossing_track(50))
    for token in ("NaN", "Infinity", "-Infinity"):
        raw = (f'{{"waypoints": {waypoints}, "zones": {zones}, '
               f'"altitude_offset": {token}}}')
        r = client.post("/audit/counterfactual", content=raw,
                        headers={"content-type": "application/json"})
        assert r.status_code == 422, token


def test_out_of_order_waypoints_422():
    r = counterfactual([wp(0, 0, 0, "2026-09-22T10:05:00Z"),
                        wp(1, 1, 1, "2026-09-22T10:00:00Z")], 10)
    assert r.status_code == 422
    assert "时间" in r.json()["detail"]


def test_invalid_zone_geometry_422():
    zone = copy.deepcopy(ZONE)
    zone["geometry"] = {  # 蝴蝶结（自相交）
        "type": "Polygon",
        "coordinates": [[[0, 0], [10, 10], [10, 0], [0, 10], [0, 0]]],
    }
    r = counterfactual(crossing_track(50), 10, [zone])
    assert r.status_code == 422
    assert "nfz-1" in r.json()["detail"]
