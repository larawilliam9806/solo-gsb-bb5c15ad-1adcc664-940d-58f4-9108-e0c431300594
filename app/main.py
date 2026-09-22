"""FastAPI 应用入口。"""
from __future__ import annotations

import math
from typing import Any, List

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .audit import TrackOrderError, ZoneGeometryError, audit_track
from .counterfactual import counterfactual_audit
from .geojson import violations_to_feature_collection
from .maintenance import TicketError, store as ticket_store
from .models import (
    AuditRequest,
    AuditResponse,
    CounterfactualRequest,
    CounterfactualResponse,
    GeoJsonFeatureCollection,
    MaintenanceTicket,
    MaintenanceTicketCreate,
    OverdueMaintenanceTicket,
    TicketTransitionRequest,
)

app = FastAPI(
    title="无人机航迹禁飞区审计",
    version="1.3.0",
    description=(
        "按航段（而非仅航点）审计无人机航迹与禁飞区的冲突："
        "平面、生效时段、高度范围三者同时命中才判定违规，"
        "并区分擦边接触（graze）与真正进入（entry）。"
    ),
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _sanitize_error_detail(obj: Any) -> Any:
    """递归替换校验错误详情中的非有限浮点数（NaN/±Inf）。

    默认 422 处理器把出错的输入原样放进响应，而非有限浮点数无法被
    JSONResponse（allow_nan=False）序列化，会把本应返回的 422 变成 500。
    """
    if isinstance(obj, float) and not math.isfinite(obj):
        return repr(obj)
    if isinstance(obj, dict):
        return {k: _sanitize_error_detail(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_error_detail(v) for v in obj]
    return obj


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # 与 FastAPI 默认处理器一致，仅先做非有限浮点数清洗
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder({"detail": _sanitize_error_detail(exc.errors())}),
    )


@app.post("/audit", response_model=AuditResponse)
def audit(req: AuditRequest) -> AuditResponse:
    try:
        violations = audit_track(req.waypoints, req.zones)
    except (TrackOrderError, ZoneGeometryError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AuditResponse(violation_count=len(violations), violations=violations)


@app.post("/audit/geojson", response_model=GeoJsonFeatureCollection)
def audit_geojson(req: AuditRequest) -> GeoJsonFeatureCollection:
    """与 /audit 相同的请求体与判定逻辑，结果以 GeoJSON FeatureCollection 返回。"""
    try:
        violations = audit_track(req.waypoints, req.zones)
    except (TrackOrderError, ZoneGeometryError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return violations_to_feature_collection(violations, req.waypoints)


@app.post("/audit/counterfactual", response_model=CounterfactualResponse)
def audit_counterfactual(req: CounterfactualRequest) -> CounterfactualResponse:
    """反事实审计：全部航点高度平移 altitude_offset 米后重新审计，并与基线结果比对。

    返回 baseline（原始航迹）、shifted（平移后航迹）两次完整审计结果，以及按
    (zone_id, entry_time, exit_time) 匹配求得的 cleared（平移后消除）与
    introduced（平移后引入）两组违规摘要。不修改请求中的航点数据。
    """
    try:
        return counterfactual_audit(req.waypoints, req.zones, req.altitude_offset)
    except (TrackOrderError, ZoneGeometryError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ----------------------------------------------------------- 设备维护工单

@app.post("/maintenance/tickets", response_model=MaintenanceTicket, status_code=201)
def create_maintenance_ticket(req: MaintenanceTicketCreate, response: Response) -> MaintenanceTicket:
    """创建设备维护工单。

    相同 idempotency_key 重复提交时返回首次创建的工单且不重复创建（HTTP 200）；
    首次创建返回 HTTP 201。
    """
    try:
        ticket, created = ticket_store.create(
            device_id=req.device_id,
            description=req.description,
            priority=req.priority,
            sla_hours=req.sla_hours,
            idempotency_key=req.idempotency_key,
        )
    except TicketError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    if not created:
        response.status_code = 200
    return ticket


@app.post(
    "/maintenance/tickets/{ticket_id}/transition",
    response_model=MaintenanceTicket,
)
def transition_maintenance_ticket(ticket_id: int, req: TicketTransitionRequest) -> MaintenanceTicket:
    """推进工单状态，只能沿 open→assigned→in_progress→resolved→closed 前进一步。

    closed 为终态；进入 closed 必须先经过 resolved（resolved_at 已写入）。
    非法转换、关闭终态工单返回 409，工单不存在返回 404。
    """
    try:
        return ticket_store.transition(ticket_id, req.to)
    except TicketError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@app.get("/maintenance/tickets/overdue", response_model=List[OverdueMaintenanceTicket])
def list_overdue_maintenance_tickets() -> List[OverdueMaintenanceTicket]:
    """超过 SLA 且尚未关闭的工单，按逾期时长（overdue_seconds）降序返回。

    已关闭或尚未逾期的工单不在列表中；无逾期工单时返回空列表。
    """
    return ticket_store.overdue()
