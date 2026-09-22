"""设备维护工单模块的端到端测试：创建、幂等、状态机与 SLA 逾期。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import maintenance
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_store():
    maintenance.store.reset()
    yield
    maintenance.store.reset()


class FakeClock:
    """可手动推进的 UTC 时钟，替换 maintenance.utcnow。"""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def create_ticket(**overrides):
    payload = {
        "device_id": "drone-007",
        "description": "左前电机起飞时异响",
        "priority": "high",
        "sla_hours": 24,
        "idempotency_key": "key-1",
    }
    payload.update(overrides)
    return client.post("/maintenance/tickets", json=payload)


def transition(ticket_id, to):
    return client.post(f"/maintenance/tickets/{ticket_id}/transition", json={"to": to})


# ---------------------------------------------------------------- 创建

def test_create_ticket_returns_201_and_fields():
    r = create_ticket()
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == 1
    assert body["device_id"] == "drone-007"
    assert body["priority"] == "high"
    assert body["sla_hours"] == 24.0
    assert body["status"] == "open"
    assert body["resolved_at"] is None and body["closed_at"] is None
    created = datetime.fromisoformat(body["created_at"])
    due = datetime.fromisoformat(body["sla_due_at"])
    assert due - created == timedelta(hours=24)
    assert created.tzinfo is not None  # UTC aware


def test_create_ticket_increments_ids():
    r1 = create_ticket(idempotency_key="a")
    r2 = create_ticket(idempotency_key="b")
    assert r1.status_code == r2.status_code == 201
    assert r2.json()["id"] == r1.json()["id"] + 1


def test_invalid_priority_422():
    r = create_ticket(priority="urgent")
    assert r.status_code == 422


def test_sla_hours_must_be_positive_finite():
    for bad in (0, -1):
        assert create_ticket(sla_hours=bad, idempotency_key=f"k-{bad}").status_code == 422
    raw = '{"device_id": "d", "description": "x", "priority": "low", ' \
          '"sla_hours": NaN, "idempotency_key": "nan"}'
    r = client.post("/maintenance/tickets", content=raw,
                    headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_missing_fields_422():
    r = client.post("/maintenance/tickets", json={"device_id": "d"})
    assert r.status_code == 422


# ---------------------------------------------------------------- 幂等

def test_idempotency_key_returns_first_ticket_without_duplicate():
    r1 = create_ticket(idempotency_key="dup", device_id="drone-007")
    assert r1.status_code == 201
    first = r1.json()

    # 重复键即使携带不同内容，也返回首次工单且不新建
    r2 = create_ticket(idempotency_key="dup", device_id="drone-999",
                       description="完全不同的问题", priority="low", sla_hours=1)
    assert r2.status_code == 200
    assert r2.json() == first

    r3 = create_ticket(idempotency_key="other")
    assert r3.status_code == 201
    assert r3.json()["id"] == 2  # 重复请求没有占用新 id


# ---------------------------------------------------------------- 状态机

def test_full_lifecycle_forward_transitions():
    ticket_id = create_ticket().json()["id"]
    for to in ("assigned", "in_progress", "resolved", "closed"):
        r = transition(ticket_id, to)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == to
    final = client.get("/maintenance/tickets/overdue")  # 顺带确认关闭后不报逾期
    assert final.status_code == 200
    assert all(t["id"] != ticket_id for t in final.json())


def test_cannot_skip_statuses():
    ticket_id = create_ticket().json()["id"]
    r = transition(ticket_id, "in_progress")  # open 不能直接跳
    assert r.status_code == 409
    assert "open" in r.json()["detail"] and "in_progress" in r.json()["detail"]
    # 失败的转换不得改变状态
    assert transition(ticket_id, "assigned").status_code == 200


def test_cannot_go_backward():
    ticket_id = create_ticket().json()["id"]
    transition(ticket_id, "assigned")
    r = transition(ticket_id, "open")
    assert r.status_code == 409


def test_closed_is_terminal():
    ticket_id = create_ticket().json()["id"]
    for to in ("assigned", "in_progress", "resolved", "closed"):
        transition(ticket_id, to)
    r = transition(ticket_id, "closed")
    assert r.status_code == 409


def test_transition_sets_timestamps_in_order():
    ticket_id = create_ticket().json()["id"]
    transition(ticket_id, "assigned")
    body = transition(ticket_id, "in_progress").json()
    assert body["assigned_at"] is not None
    assert body["in_progress_at"] >= body["assigned_at"]
    assert body["resolved_at"] is None
    body = transition(ticket_id, "resolved").json()
    assert body["resolved_at"] is not None
    assert body["closed_at"] is None
    body = transition(ticket_id, "closed").json()
    assert body["closed_at"] is not None
    # 关闭必须在 resolved 之后：resolved_at 已先写入
    assert body["resolved_at"] is not None
    assert body["closed_at"] >= body["resolved_at"]


def test_transition_unknown_ticket_404():
    r = client.post("/maintenance/tickets/999/transition", json={"to": "assigned"})
    assert r.status_code == 404


# ---------------------------------------------------------------- SLA 逾期

def test_overdue_lists_only_unclosed_past_sla_sorted_desc():
    clock = FakeClock(datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(maintenance, "utcnow", clock)
    try:
        # A：SLA 2h；B：SLA 48h（不会逾期）；C：SLA 1h 但随后关闭
        a = create_ticket(idempotency_key="a", sla_hours=2).json()["id"]
        b = create_ticket(idempotency_key="b", sla_hours=48).json()["id"]
        c = create_ticket(idempotency_key="c", sla_hours=1).json()["id"]
        for to in ("assigned", "in_progress", "resolved", "closed"):
            assert transition(c, to).status_code == 200

        # 5 小时后：A 逾期 3h；B 未到期；C 已关闭
        clock.advance(hours=5)
        body = client.get("/maintenance/tickets/overdue").json()
        assert [t["id"] for t in body] == [a]
        assert body[0]["overdue_seconds"] == pytest.approx(3 * 3600.0)

        # D 在此时创建，SLA 8h；再过 10h：A 逾期 13h，D 逾期 2h，降序返回
        d = create_ticket(idempotency_key="d", sla_hours=8).json()["id"]
        clock.advance(hours=10)
        body = client.get("/maintenance/tickets/overdue").json()
        assert [t["id"] for t in body] == [a, d]
        assert body[0]["overdue_seconds"] > body[1]["overdue_seconds"]
        assert b not in [t["id"] for t in body]
    finally:
        monkeypatch.undo()


def test_overdue_empty_returns_empty_list():
    r = client.get("/maintenance/tickets/overdue")
    assert r.status_code == 200
    assert r.json() == []


def test_exactly_at_sla_boundary_is_not_overdue():
    clock = FakeClock(datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc))
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(maintenance, "utcnow", clock)
    try:
        create_ticket(idempotency_key="boundary", sla_hours=24)
        clock.advance(hours=24)
        assert client.get("/maintenance/tickets/overdue").json() == []
        clock.advance(seconds=1)
        body = client.get("/maintenance/tickets/overdue").json()
        assert len(body) == 1
        assert body[0]["overdue_seconds"] == pytest.approx(1.0)
    finally:
        monkeypatch.undo()
        maintenance.store.reset()
