"""设备维护工单的端到端测试：创建幂等、单向状态机、关闭前置 resolved、SLA 逾期排序。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import maintenance
from app.main import app

client = TestClient(app)

T0 = datetime(2026, 9, 22, 8, 0, 0, tzinfo=timezone.utc)


def make_payload(**overrides):
    payload = {
        "device_id": "drone-uav-007",
        "description": "左前电机悬停异响",
        "priority": "high",
        "sla_hours": 24,
        "idempotency_key": "k-default",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def frozen_clock(monkeypatch):
    """可控时钟：tick() 推进后，后续取时统一返回当前时钟值。"""

    class Clock:
        def __init__(self):
            self.now = T0

        def __call__(self):
            return self.now

        def tick(self, hours=0, seconds=0):
            self.now += timedelta(hours=hours, seconds=seconds)
            return self.now

    clock = Clock()
    monkeypatch.setattr(maintenance, "utc_now", clock)
    maintenance.store.reset()
    yield clock
    maintenance.store.reset()


# ---------------------------------------------------------------- 创建与幂等

def test_create_ticket_returns_201_with_open_state(frozen_clock):
    r = client.post("/maintenance/tickets", json=make_payload())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "open"
    assert body["device_id"] == "drone-uav-007"
    assert body["priority"] == "high"
    assert body["created_at"] == "2026-09-22T08:00:00Z"
    assert body["sla_deadline"] == "2026-09-23T08:00:00Z"
    assert body["assigned_at"] is None
    assert body["resolved_at"] is None
    assert body["closed_at"] is None
    assert isinstance(body["id"], str) and len(body["id"]) == 32


def test_default_priority_is_normal(frozen_clock):
    payload = make_payload()
    del payload["priority"]
    body = client.post("/maintenance/tickets", json=payload).json()
    assert body["priority"] == "normal"


def test_idempotent_repeat_returns_first_ticket_with_200(frozen_clock):
    payload = make_payload(idempotency_key="idem-1")
    r1 = client.post("/maintenance/tickets", json=payload)
    assert r1.status_code == 201
    first_id = r1.json()["id"]

    # 重复请求，即使请求体其它字段不同也只认首次
    r2 = client.post(
        "/maintenance/tickets",
        json=make_payload(idempotency_key="idem-1", device_id="other-device", description="别的问题"),
    )
    assert r2.status_code == 200
    assert r2.json()["id"] == first_id
    assert r2.json()["device_id"] == "drone-uav-007"
    assert r2.json()["description"] == "左前电机悬停异响"


def test_different_idempotency_keys_create_distinct_tickets(frozen_clock):
    r1 = client.post("/maintenance/tickets", json=make_payload(idempotency_key="k-1"))
    r2 = client.post("/maintenance/tickets", json=make_payload(idempotency_key="k-2"))
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]


def test_concurrent_requests_with_same_key_create_only_one(frozen_clock):
    """并发同幂等键：必须只创建一张工单（锁的原子性）。"""
    from concurrent.futures import ThreadPoolExecutor

    payload = make_payload(idempotency_key="race-1")

    def post():
        r = client.post("/maintenance/tickets", json=payload)
        return r.status_code, r.json()["id"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: post(), range(16)))

    ids = {ticket_id for _, ticket_id in results}
    assert len(ids) == 1
    assert sum(1 for code, _ in results if code == 201) == 1
    assert sum(1 for code, _ in results if code == 200) == 15


def test_create_ticket_validation_errors(frozen_clock):
    bad_device = client.post("/maintenance/tickets", json=make_payload(device_id=""))
    assert bad_device.status_code == 422

    bad_desc = client.post("/maintenance/tickets", json=make_payload(description=""))
    assert bad_desc.status_code == 422

    bad_priority = client.post("/maintenance/tickets", json=make_payload(priority="P0"))
    assert bad_priority.status_code == 422

    bad_sla = client.post("/maintenance/tickets", json=make_payload(sla_hours=0))
    assert bad_sla.status_code == 422
    neg_sla = client.post("/maintenance/tickets", json=make_payload(sla_hours=-1))
    assert neg_sla.status_code == 422

    missing_key = make_payload()
    del missing_key["idempotency_key"]
    assert client.post("/maintenance/tickets", json=missing_key).status_code == 422


# ---------------------------------------------------------------- 状态推进

def _create(client, key="k-1", **overrides):
    return client.post(
        "/maintenance/tickets", json=make_payload(idempotency_key=key, **overrides)
    ).json()


def test_full_lifecycle_forward_in_order(frozen_clock):
    tid = _create(client)["id"]

    r = client.post(f"/maintenance/tickets/{tid}/transition", json={"status": "assigned"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "assigned"
    assert r.json()["assigned_at"] == "2026-09-22T08:00:00Z"

    frozen_clock.tick(hours=1)
    r = client.post(f"/maintenance/tickets/{tid}/transition", json={"status": "in_progress"})
    assert r.json()["status"] == "in_progress"
    assert r.json()["in_progress_at"] == "2026-09-22T09:00:00Z"
    # 早前的状态时间戳保留
    assert r.json()["assigned_at"] == "2026-09-22T08:00:00Z"

    frozen_clock.tick(hours=1)
    r = client.post(f"/maintenance/tickets/{tid}/transition", json={"status": "resolved"})
    assert r.json()["status"] == "resolved"
    assert r.json()["resolved_at"] == "2026-09-22T10:00:00Z"

    frozen_clock.tick(hours=1)
    r = client.post(f"/maintenance/tickets/{tid}/transition", json={"status": "closed"})
    assert r.json()["status"] == "closed"
    assert r.json()["closed_at"] == "2026-09-22T11:00:00Z"
    # 关闭前必须已有 resolved_at
    assert r.json()["resolved_at"] == "2026-09-22T10:00:00Z"


def test_cannot_skip_statuses(frozen_clock):
    tid = _create(client)["id"]
    for target in ("in_progress", "resolved", "closed"):
        r = client.post(f"/maintenance/tickets/{tid}/transition", json={"status": target})
        assert r.status_code == 409
        assert "open" in r.json()["detail"]
    # 被拒后工单仍停留在 open
    assert client.post(
        f"/maintenance/tickets/{tid}/transition", json={"status": "assigned"}
    ).status_code == 200


def test_cannot_close_before_resolved(frozen_clock):
    tid = _create(client)["id"]
    # open → closed
    r = client.post(f"/maintenance/tickets/{tid}/transition", json={"status": "closed"})
    assert r.status_code == 409
    assert client.get("/maintenance/tickets/overdue") is not None  # 工单仍为 open，未被关闭

    # assigned → closed 同样禁止
    client.post(f"/maintenance/tickets/{tid}/transition", json={"status": "assigned"})
    r = client.post(f"/maintenance/tickets/{tid}/transition", json={"status": "closed"})
    assert r.status_code == 409

    # in_progress → closed 同样禁止（resolved_at 尚为空）
    client.post(f"/maintenance/tickets/{tid}/transition", json={"status": "in_progress"})
    r = client.post(f"/maintenance/tickets/{tid}/transition", json={"status": "closed"})
    assert r.status_code == 409

    # 被拒推进不改变状态与时间戳
    body = client.post(
        f"/maintenance/tickets/{tid}/transition", json={"status": "resolved"}
    ).json()
    assert body["status"] == "resolved"
    assert body["resolved_at"] is not None
    assert body["closed_at"] is None


def test_cannot_go_backwards_or_repeat(frozen_clock):
    tid = _create(client)["id"]
    client.post(f"/maintenance/tickets/{tid}/transition", json={"status": "assigned"})

    # 回退
    r = client.post(f"/maintenance/tickets/{tid}/transition", json={"status": "open"})
    assert r.status_code == 409
    # 重复推进同一状态
    r = client.post(f"/maintenance/tickets/{tid}/transition", json={"status": "assigned"})
    assert r.status_code == 409

    # 走到终态后不能再推进
    for target in ("in_progress", "resolved", "closed"):
        assert client.post(
            f"/maintenance/tickets/{tid}/transition", json={"status": target}
        ).status_code == 200
    for target in ("open", "assigned", "in_progress", "resolved", "closed"):
        r = client.post(f"/maintenance/tickets/{tid}/transition", json={"status": target})
        assert r.status_code == 409


def test_transition_unknown_ticket_404(frozen_clock):
    r = client.post(
        "/maintenance/tickets/does-not-exist/transition", json={"status": "assigned"}
    )
    assert r.status_code == 404


def test_transition_validation_error(frozen_clock):
    tid = _create(client)["id"]
    r = client.post(f"/maintenance/tickets/{tid}/transition", json={"status": "bogus"})
    assert r.status_code == 422
    r = client.post(f"/maintenance/tickets/{tid}/transition", json={})
    assert r.status_code == 422


# ---------------------------------------------------------------- SLA 逾期

def test_overdue_empty_before_deadline(frozen_clock):
    _create(client, "k-1", sla_hours=24)
    frozen_clock.tick(hours=23, seconds=59)
    r = client.get("/maintenance/tickets/overdue")
    assert r.status_code == 200
    assert r.json() == []


def test_overdue_listed_after_deadline(frozen_clock):
    _create(client, "k-1", sla_hours=24)
    frozen_clock.tick(hours=25)
    body = client.get("/maintenance/tickets/overdue").json()
    assert len(body) == 1
    assert body[0]["idempotency_key"] == "k-1"
    assert body[0]["overdue_seconds"] == pytest.approx(3600.0)
    assert body[0]["status"] == "open"


def test_closed_ticket_is_not_overdue_even_after_deadline(frozen_clock):
    tid = _create(client, "k-1", sla_hours=1)["id"]
    # 在 SLA 内关闭：closed 后无论过多久都不再逾期
    for target in ("assigned", "in_progress", "resolved", "closed"):
        client.post(f"/maintenance/tickets/{tid}/transition", json={"status": target})
    frozen_clock.tick(hours=48)
    assert client.get("/maintenance/tickets/overdue").json() == []


def test_resolved_but_unclosed_still_overdue(frozen_clock):
    """resolved 不等于 closed：已解决但未关闭的工单超 SLA 仍应出现。"""
    tid = _create(client, "k-1", sla_hours=1)["id"]
    for target in ("assigned", "in_progress", "resolved"):
        client.post(f"/maintenance/tickets/{tid}/transition", json={"status": target})
    frozen_clock.tick(hours=2)
    body = client.get("/maintenance/tickets/overdue").json()
    assert len(body) == 1
    assert body[0]["status"] == "resolved"
    assert body[0]["overdue_seconds"] == pytest.approx(3600.0)


def test_overdue_sorted_by_overdue_duration_desc(frozen_clock):
    # t0 建单 A：SLA 2h（截止 t0+2h）
    _create(client, "ticket-a", sla_hours=2)
    frozen_clock.tick(hours=1)
    # t0+1h 建单 B：SLA 1h（截止同样为 t0+2h）
    _create(client, "ticket-b", sla_hours=1)
    # t0+1h 再建单 C：SLA 6h（截止 t0+7h，未逾期，不应出现）
    _create(client, "ticket-c", sla_hours=6)
    frozen_clock.tick(hours=3)
    # 查询时刻 t0+4h：A、B 均逾期 2h（并列），C 未逾期
    body = client.get("/maintenance/tickets/overdue").json()
    keys = [t["idempotency_key"] for t in body]
    assert set(keys) == {"ticket-a", "ticket-b"}
    overdue = [t["overdue_seconds"] for t in body]
    assert overdue == [pytest.approx(7200.0), pytest.approx(7200.0)]
    # 并列顺序稳定：创建时间早的 A 在前
    assert keys == ["ticket-a", "ticket-b"]


def test_overdue_ordering_strict_desc_with_distinct_deadlines(frozen_clock):
    """用相同创建时间、不同 SLA 构造严格不同的逾期时长，验证降序。"""
    _create(client, "long-sla", sla_hours=10)
    _create(client, "short-sla", sla_hours=1)
    frozen_clock.tick(hours=5)
    body = client.get("/maintenance/tickets/overdue").json()
    # short: 逾期 4h（14400s）；long: 截止未到，不逾期
    assert [t["idempotency_key"] for t in body] == ["short-sla"]
    assert body[0]["overdue_seconds"] == pytest.approx(4 * 3600)

    frozen_clock.tick(hours=10)  # now = t0+15h
    body = client.get("/maintenance/tickets/overdue").json()
    # short 逾期 14h > long 逾期 5h
    assert [t["idempotency_key"] for t in body] == ["short-sla", "long-sla"]
    assert body[0]["overdue_seconds"] > body[1]["overdue_seconds"]
