"""设备维护工单：内存存储、状态机、幂等键与 SLA 逾期判定。

工单状态只能沿 open→assigned→in_progress→resolved→closed 单向推进，
且每次只能推进到紧邻的下一状态；closed 为终态。所有时间均使用 UTC。
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple

from .models import MaintenanceTicket, OverdueMaintenanceTicket

# 状态机：每次 transition 允许的唯一目标状态
NEXT_STATUS: Dict[str, str] = {
    "open": "assigned",
    "assigned": "in_progress",
    "in_progress": "resolved",
    "resolved": "closed",
}

# 每个状态对应的进入时间戳字段
STATUS_TIMESTAMP_FIELD: Dict[str, str] = {
    "assigned": "assigned_at",
    "in_progress": "in_progress_at",
    "resolved": "resolved_at",
    "closed": "closed_at",
}


class TicketError(Exception):
    """工单业务错误（HTTP 层映射为 4xx）。"""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class TicketNotFound(TicketError):
    def __init__(self, ticket_id: int) -> None:
        super().__init__(f"工单不存在: {ticket_id}", status_code=404)


class TicketStore:
    """线程安全的进程内工单存储（无外部数据库依赖）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: Dict[int, MaintenanceTicket] = {}
        self._idempotency_index: Dict[str, int] = {}
        self._next_id = 1

    def reset(self) -> None:
        with self._lock:
            self._tickets.clear()
            self._idempotency_index.clear()
            self._next_id = 1

    def create(
        self,
        device_id: str,
        description: str,
        priority: str,
        sla_hours: float,
        idempotency_key: str,
    ) -> Tuple[MaintenanceTicket, bool]:
        """创建工单。幂等键已存在时返回首次创建的工单。

        返回 (工单, 是否本次新建)。
        """
        now = utcnow()
        with self._lock:
            existing_id = self._idempotency_index.get(idempotency_key)
            if existing_id is not None:
                return self._tickets[existing_id], False
            ticket_id = self._next_id
            self._next_id += 1
            ticket = MaintenanceTicket(
                id=ticket_id,
                device_id=device_id,
                description=description,
                priority=priority,
                sla_hours=sla_hours,
                idempotency_key=idempotency_key,
                status="open",
                created_at=now,
                sla_due_at=now + timedelta(hours=sla_hours),
            )
            self._tickets[ticket_id] = ticket
            self._idempotency_index[idempotency_key] = ticket_id
            return ticket, True

    def transition(self, ticket_id: int, to: str) -> MaintenanceTicket:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise TicketNotFound(ticket_id)
            expected = NEXT_STATUS.get(ticket.status)
            if expected is None:
                raise TicketError(f"工单已关闭（{ticket.status}），不能继续推进")
            if to != expected:
                raise TicketError(
                    f"非法状态转换：{ticket.status} → {to}；"
                    f"当前只能推进到 {expected}"
                )
            now = utcnow()
            setattr(ticket, STATUS_TIMESTAMP_FIELD[to], now)
            ticket.status = to  # type: ignore[assignment]
            return ticket

    def overdue(self) -> List[OverdueMaintenanceTicket]:
        """超过 SLA 且尚未关闭的工单，按逾期时长降序（时长相同按 id 升序）。"""
        now = utcnow()
        result: List[OverdueMaintenanceTicket] = []
        with self._lock:
            for ticket in self._tickets.values():
                if ticket.status == "closed":
                    continue
                overdue_seconds = (now - ticket.sla_due_at).total_seconds()
                if overdue_seconds <= 0:
                    continue
                result.append(
                    OverdueMaintenanceTicket(
                        **ticket.model_dump(), overdue_seconds=overdue_seconds
                    )
                )
        result.sort(key=lambda t: (-t.overdue_seconds, t.id))
        return result


def utcnow() -> datetime:
    """当前 UTC 时间（独立函数便于测试 monkeypatch）。"""
    return datetime.now(timezone.utc)


store = TicketStore()
