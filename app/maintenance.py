"""设备维护工单：内存存储、幂等创建、单向状态机与 SLA 逾期查询。

设计要点：
  - 存储为进程内字典 + threading.Lock，FastAPI 同步端点会在线程池中运行，
    需要锁保证并发创建/推进的原子性（含幂等键去重）。
  - 状态只能沿 open→assigned→in_progress→resolved→closed **前进一格**，
    跳跃、回退、重复推进均返回 409。
  - closed 必须经由 resolved：resolved_at 为空时不可能关闭。
  - “超过 SLA 的未关闭工单”指 status != closed 且 now > created_at + sla_hours；
    resolved 但尚未 closed 的工单同样参与逾期统计。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from .models import OverdueTicket, Ticket, TicketCreate

# 状态机：仅允许前进到相邻的下一状态
NEXT_STATUS: Dict[str, str] = {
    "open": "assigned",
    "assigned": "in_progress",
    "in_progress": "resolved",
    "resolved": "closed",
}

# 进入某状态时需要落时间戳的字段
STATUS_TIMESTAMP_FIELD: Dict[str, str] = {
    "assigned": "assigned_at",
    "in_progress": "in_progress_at",
    "resolved": "resolved_at",
    "closed": "closed_at",
}


class TicketError(ValueError):
    """工单逻辑错误（非法状态推进）。"""


class TicketNotFoundError(KeyError):
    """工单不存在。"""


def utc_now() -> datetime:
    """统一的当前 UTC 时间入口，便于测试 monkeypatch。"""
    return datetime.now(timezone.utc)


class TicketStore:
    """线程安全的进程内工单存储（进程重启即清空）。"""

    def __init__(self) -> None:
        self._tickets: Dict[str, Ticket] = {}
        self._idempotency_index: Dict[str, str] = {}
        self._lock = Lock()

    def create(self, req: TicketCreate) -> Tuple[Ticket, bool]:
        """按幂等键创建工单。

        返回 (ticket, created)：created=False 表示命中幂等键、返回首次工单。
        """
        with self._lock:
            existing_id = self._idempotency_index.get(req.idempotency_key)
            if existing_id is not None:
                return self._tickets[existing_id], False

            ticket = Ticket(
                id=uuid4().hex,
                device_id=req.device_id,
                description=req.description,
                priority=req.priority,
                sla_hours=req.sla_hours,
                idempotency_key=req.idempotency_key,
                status="open",
                created_at=utc_now(),
            )
            self._tickets[ticket.id] = ticket
            self._idempotency_index[req.idempotency_key] = ticket.id
            return ticket, True

    def get(self, ticket_id: str) -> Ticket:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFoundError(ticket_id)
            return ticket.model_copy(deep=True)

    def transition(self, ticket_id: str, target: str) -> Ticket:
        """把工单推进到 target 状态；非法推进抛 TicketError。"""
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFoundError(ticket_id)

            expected = NEXT_STATUS.get(ticket.status)
            if target != expected:
                if expected is None:
                    reason = f"工单已关闭（{ticket.status}），不可再推进"
                else:
                    reason = (
                        f"状态只能按 open→assigned→in_progress→resolved→closed 前进："
                        f"当前为 {ticket.status}，下一状态必须是 {expected}，不能推进到 {target}"
                    )
                raise TicketError(reason)

            now = utc_now()
            updated = ticket.model_copy(
                update={"status": target, STATUS_TIMESTAMP_FIELD[target]: now}
            )
            self._tickets[ticket_id] = updated
            return updated.model_copy(deep=True)

    def overdue(self, now: Optional[datetime] = None) -> List[OverdueTicket]:
        """返回超过 SLA 的未关闭工单，按逾期时长降序。"""
        if now is None:
            now = utc_now()
        with self._lock:
            snapshots = [t.model_copy(deep=True) for t in self._tickets.values()]

        result: List[OverdueTicket] = []
        for t in snapshots:
            if t.status == "closed":
                continue
            overdue_seconds = (now - t.sla_deadline).total_seconds()
            if overdue_seconds > 0:
                result.append(
                    OverdueTicket(**t.model_dump(), overdue_seconds=overdue_seconds)
                )
        # 逾期时长降序；并列时创建时间早的（通常也更该先处理）在前，再以 id 兜底保证稳定
        result.sort(key=lambda t: (-t.overdue_seconds, t.created_at, t.id))
        return result

    def reset(self) -> None:
        """仅供测试：清空全部工单。"""
        with self._lock:
            self._tickets.clear()
            self._idempotency_index.clear()


store = TicketStore()
