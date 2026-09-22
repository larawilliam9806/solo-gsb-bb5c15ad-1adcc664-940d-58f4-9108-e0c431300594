"""请求 / 响应的数据模型（Pydantic v2）。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _ensure_utc(v: datetime) -> datetime:
    """无 tzinfo 的时间一律按 UTC 处理，避免 naive/aware 混比。"""
    if v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


class Waypoint(BaseModel):
    lat: float = Field(..., ge=-90.0, le=90.0, description="纬度，度")
    lon: float = Field(..., ge=-180.0, le=180.0, description="经度，度")
    alt: float = Field(..., description="高度，米")
    time: datetime = Field(..., description="ISO 8601 时间，无时区按 UTC")

    _utc = field_validator("time")(_ensure_utc)


class Zone(BaseModel):
    """禁飞区：GeoJSON 多边形 + 生效时段 + 高度范围。"""

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "id": "nfz-1",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[116.30, 39.90], [116.32, 39.90],
                                 [116.32, 39.92], [116.30, 39.92], [116.30, 39.90]]],
            },
            "active_from": "2026-09-22T10:00:00Z",
            "active_to": "2026-09-22T11:00:00Z",
            "min_alt": 0,
            "max_alt": 120,
        }
    })

    id: str = Field(..., min_length=1, description="区域标识")
    geometry: Dict[str, Any] = Field(..., description="GeoJSON Polygon / MultiPolygon")
    active_from: datetime
    active_to: datetime
    min_alt: Optional[float] = Field(None, description="高度下限（含），缺省不限制")
    max_alt: Optional[float] = Field(None, description="高度上限（含），缺省不限制")

    _utc_from = field_validator("active_from")(_ensure_utc)
    _utc_to = field_validator("active_to")(_ensure_utc)

    @model_validator(mode="after")
    def _check_window(self) -> "Zone":
        if self.active_to <= self.active_from:
            raise ValueError("active_to 必须晚于 active_from")
        if self.min_alt is not None and self.max_alt is not None and self.max_alt < self.min_alt:
            raise ValueError("max_alt 不能小于 min_alt")
        return self


class AuditRequest(BaseModel):
    waypoints: List[Waypoint] = Field(..., min_length=2, description="按时间升序的航点")
    zones: List[Zone] = Field(default_factory=list, description="禁飞区列表")


class CounterfactualRequest(AuditRequest):
    """在 /audit 请求基础上增加必填的高度平移量（反事实审计）。"""

    altitude_offset: float = Field(
        ...,
        allow_inf_nan=False,
        description="高度平移量（米）：正数整体抬高航迹，负数整体降低",
    )


class Position(BaseModel):
    lat: float
    lon: float
    alt: float


class Violation(BaseModel):
    zone_id: str
    kind: Literal["entry", "graze"] = Field(..., description="entry=真正进入；graze=擦边接触")
    entry_time: datetime
    exit_time: datetime
    entry_position: Position
    exit_position: Position
    segment_start: int = Field(..., description="进入点所在的航段下标（从 0 开始）")
    segment_end: int = Field(..., description="离开点所在的航段下标")


class AuditResponse(BaseModel):
    violation_count: int
    violations: List[Violation]


class CounterfactualResponse(BaseModel):
    """反事实审计结果：baseline/shifted 为两次完整审计，cleared/introduced 为差集摘要。

    匹配键为 (zone_id, entry_time, exit_time)：基线中存在而平移后消失的记入
    cleared，平移后才出现的记入 introduced。
    """

    altitude_offset: float = Field(..., description="实际使用的高度平移量（米）")
    baseline: AuditResponse = Field(..., description="原始航迹的审计结果")
    shifted: AuditResponse = Field(..., description="全部航点高度平移后的审计结果")
    cleared_count: int = Field(..., description="平移后消除的违规条数")
    introduced_count: int = Field(..., description="平移后新引入的违规条数")
    cleared: List[Violation] = Field(..., description="基线中存在、平移后不再出现的违规")
    introduced: List[Violation] = Field(..., description="平移后才出现的违规")


class ViolationProperties(BaseModel):
    """GeoJSON Feature 的属性，与 Violation 的标量字段一一对应。"""

    zone_id: str
    kind: Literal["entry", "graze"]
    entry_time: datetime
    exit_time: datetime
    segment_start: int
    segment_end: int


class GeoJsonFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: Dict[str, Any] = Field(
        ..., description="Point 或 LineString，坐标为 [经度, 纬度, 高度]"
    )
    properties: ViolationProperties


class GeoJsonFeatureCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: List[GeoJsonFeature]


# ------------------------------------------------------------- 设备维护工单

TicketPriority = Literal["low", "medium", "high", "critical"]
TicketStatus = Literal["open", "assigned", "in_progress", "resolved", "closed"]


class MaintenanceTicketCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "device_id": "drone-007",
            "description": "左前电机起飞时异响，震动明显",
            "priority": "high",
            "sla_hours": 24,
            "idempotency_key": "ticket-20260922-0001",
        }
    })

    device_id: str = Field(..., min_length=1, description="设备标识")
    description: str = Field(..., min_length=1, description="问题描述")
    priority: TicketPriority = Field(..., description="优先级")
    sla_hours: float = Field(
        ..., gt=0, allow_inf_nan=False, description="SLA 时限（小时），必须为正有限数值"
    )
    idempotency_key: str = Field(
        ..., min_length=1, description="幂等键；相同键重复提交返回首次创建的工单，不重复创建"
    )


class TicketTransitionRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"to": "assigned"}})

    to: TicketStatus = Field(
        ..., description="目标状态，只能推进到当前状态在状态机中的下一状态"
    )


class MaintenanceTicket(BaseModel):
    id: int
    device_id: str
    description: str
    priority: TicketPriority
    sla_hours: float
    idempotency_key: str
    status: TicketStatus = Field(..., description="open→assigned→in_progress→resolved→closed")
    created_at: datetime
    sla_due_at: datetime = Field(..., description="created_at + sla_hours")
    assigned_at: Optional[datetime] = None
    in_progress_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None


class OverdueMaintenanceTicket(MaintenanceTicket):
    overdue_seconds: float = Field(
        ..., description="逾期秒数（当前时间 - sla_due_at），逾期列表按其降序"
    )
