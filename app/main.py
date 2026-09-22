"""FastAPI 应用入口。"""
from __future__ import annotations

import math
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .audit import TrackOrderError, ZoneGeometryError, audit_track
from .counterfactual import counterfactual_audit
from .geojson import violations_to_feature_collection
from .maintenance import TicketError, TicketNotFoundError, store as ticket_store
from .models import (
    AuditRequest,
    AuditResponse,
    CounterfactualRequest,
    CounterfactualResponse,
    GeoJsonFeatureCollection,
    OverdueTicket,
    Ticket,
    TicketCreate,
    TicketTransitionRequest,
)

app = FastAPI(
    title="无人机航迹禁飞区审计",
    version="1.3.0",
    description=(
        "按航段（而非仅航点）审计无人机航迹与禁飞区的冲突："
        "平面、生效时段、高度范围三者同时命中才判定违规，"
        "并区分擦边接触（graze）与真正进入（entry）。"
        "另提供设备维护工单（/maintenance/tickets）的创建、状态推进与 SLA 逾期查询。"
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


# ================================================================
# 设备维护工单
# ================================================================

_MAINTENANCE_TAG = "设备维护工单"


@app.post(
    "/maintenance/tickets",
    response_model=Ticket,
    status_code=201,
    tags=[_MAINTENANCE_TAG],
    summary="创建维护工单（幂等）",
    responses={
        200: {"description": "幂等命中：返回首次创建的工单，未重复创建", "model": Ticket},
        201: {"description": "工单已创建", "model": Ticket},
    },
)
def create_ticket(response: Response, req: TicketCreate) -> Ticket:
    """创建工单。相同 `idempotency_key` 的重复请求返回**首次**创建的工单。

    - 首次请求返回 `201`；幂等键命中返回 `200`，且不会创建第二张工单。
    - 工单初始状态为 `open`，`sla_deadline = created_at + sla_hours`。
    """
    ticket, created = ticket_store.create(req)
    if not created:
        response.status_code = 200
    return ticket


@app.post(
    "/maintenance/tickets/{ticket_id}/transition",
    response_model=Ticket,
    tags=[_MAINTENANCE_TAG],
    summary="推进工单状态",
    responses={
        404: {"description": "工单不存在"},
        409: {"description": "非法状态推进（跳跃、回退或已关闭）"},
    },
)
def transition_ticket(ticket_id: str, req: TicketTransitionRequest) -> Ticket:
    """把工单状态推进到 `status`。

    只能按 **open→assigned→in_progress→resolved→closed** 前进一格：
    跳跃、回退、重复推进同一状态返回 `409`。
    进入 `resolved` 时记录 `resolved_at`；进入 `closed` 时记录 `closed_at`
    （因此关闭前必然已有 `resolved_at`）。
    """
    try:
        return ticket_store.transition(ticket_id, req.status)
    except TicketNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"工单不存在: {exc.args[0]}") from exc
    except TicketError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get(
    "/maintenance/tickets/overdue",
    response_model=list[OverdueTicket],
    tags=[_MAINTENANCE_TAG],
    summary="查询超 SLA 的未关闭工单",
)
def overdue_tickets() -> list[OverdueTicket]:
    """返回**超过 SLA 且尚未关闭**的工单（含已 resolved 但未 closed）。

    按逾期时长（`overdue_seconds = now - sla_deadline`）**降序**排列；
    已关闭或尚未逾期的工单不返回。
    """
    return ticket_store.overdue()
