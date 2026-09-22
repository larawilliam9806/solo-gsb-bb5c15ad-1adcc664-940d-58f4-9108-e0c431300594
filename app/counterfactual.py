"""反事实高度平移审计：将全部航点高度统一平移后重新执行相同审计，再与基线结果做差集。

匹配键为 (zone_id, entry_time, exit_time)：
  - cleared    —— 基线中存在、平移后不再出现的违规；
  - introduced —— 平移后才出现的违规。
注意平移会改变高度窗口与航段分数域的交集，幸存违规的 entry/exit 时刻可能随之改变；
时刻发生变化的违规按“一条 cleared + 一条 introduced”呈现，这正是匹配键的语义。

差集采用多重集语义（按匹配键计数），且输出按固定键排序，保证相同输入得到稳定一致的结果。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import List, Tuple

from .audit import audit_track
from .models import (
    AuditResponse,
    CounterfactualResponse,
    Violation,
    Waypoint,
    Zone,
)

# 差集输出的稳定排序键：时间优先，其次区域与类别，保证相同输入顺序完全一致
_SORT_KEY = lambda v: (v.entry_time, v.exit_time, v.zone_id, v.kind)  # noqa: E731


def shift_waypoints(waypoints: List[Waypoint], altitude_offset: float) -> List[Waypoint]:
    """返回高度统一平移后的新航点列表；不修改传入的航点对象。"""
    return [
        Waypoint(lat=w.lat, lon=w.lon, alt=w.alt + altitude_offset, time=w.time)
        for w in waypoints
    ]


def _match_key(v: Violation) -> Tuple[str, datetime, datetime]:
    return (v.zone_id, v.entry_time, v.exit_time)


def _difference(mine: List[Violation], theirs: List[Violation]) -> List[Violation]:
    """多重集差：mine 中匹配键未在 theirs 出现（或出现次数更多）的违规，稳定排序。"""
    remaining = Counter(_match_key(v) for v in theirs)
    out: List[Violation] = []
    for v in mine:
        key = _match_key(v)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            out.append(v)
    out.sort(key=_SORT_KEY)
    return out


def counterfactual_audit(
    waypoints: List[Waypoint], zones: List[Zone], altitude_offset: float
) -> CounterfactualResponse:
    """先审计原始航迹，再审计高度平移后的航迹，最后按匹配键求两组差集。"""
    baseline = audit_track(waypoints, zones)
    shifted = audit_track(shift_waypoints(waypoints, altitude_offset), zones)

    cleared = _difference(baseline, shifted)
    introduced = _difference(shifted, baseline)

    return CounterfactualResponse(
        altitude_offset=altitude_offset,
        baseline=AuditResponse(violation_count=len(baseline), violations=baseline),
        shifted=AuditResponse(violation_count=len(shifted), violations=shifted),
        cleared_count=len(cleared),
        introduced_count=len(introduced),
        cleared=cleared,
        introduced=introduced,
    )
