# 无人机航迹禁飞区审计服务

基于 FastAPI + Shapely 的航迹合规审计服务。输入按时间排序的航点序列与带生效时段、高度范围的 GeoJSON 禁飞区，输出按时间排序的违规区间。

## 快速启动（单条命令，端口 8000）

```bash
docker compose up --build
```

服务启动后监听 **8000** 端口：

- 交互式文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

不使用 Docker 时：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install -r requirements-dev.txt
pytest
```

## 接口

### POST /audit

**请求体**

```json
{
  "waypoints": [
    {"lat": 39.905, "lon": 116.29, "alt": 50, "time": "2026-09-22T10:00:00Z"},
    {"lat": 39.905, "lon": 116.34, "alt": 50, "time": "2026-09-22T10:30:00Z"}
  ],
  "zones": [
    {
      "id": "nfz-1",
      "geometry": {
        "type": "Polygon",
        "coordinates": [[[116.30, 39.90], [116.32, 39.90], [116.32, 39.92], [116.30, 39.92], [116.30, 39.90]]]
      },
      "active_from": "2026-09-22T10:00:00Z",
      "active_to": "2026-09-22T11:00:00Z",
      "min_alt": 0,
      "max_alt": 120
    }
  ]
}
```

- `waypoints`：至少 2 个，必须按 `time` **严格递增**；`time` 为 ISO 8601（无时区按 UTC 处理）。
- `zones[].geometry`：GeoJSON `Polygon` 或 `MultiPolygon`（坐标顺序为 `[经度, 纬度]`）。
- `min_alt` / `max_alt`：高度范围（含端点），缺省表示不限制。

**响应 200**

```json
{
  "violation_count": 1,
  "violations": [
    {
      "zone_id": "nfz-1",
      "kind": "entry",
      "entry_time": "2026-09-22T10:06:00Z",
      "exit_time": "2026-09-22T10:18:00Z",
      "entry_position": {"lat": 39.905, "lon": 116.30, "alt": 50.0},
      "exit_position": {"lat": 39.905, "lon": 116.32, "alt": 50.0},
      "segment_start": 0,
      "segment_end": 0
    }
  ]
}
```

- `violations` 按 `entry_time` 升序排列。
- `kind = "entry"`：真正进入区域内部；`kind = "graze"`：擦边接触（掠过顶点、沿边界滑行，或仅在边界时刻触碰）。graze 的进入/离开位置可能相同。
- 进入/离开的时间与位置（含高度）均按航段线性插值求得。

**响应 422**（请求无法审计）

- 航点未按时间严格递增（含时间相同）；
- 多边形无效（自相交、面积为零、类型不是 Polygon/MultiPolygon、GeoJSON 无法解析）；
- `active_to` 不晚于 `active_from`、`max_alt` 小于 `min_alt`、航点少于 2 个等参数错误。

### POST /audit/geojson

**请求体**与 `POST /audit` 完全相同，判定逻辑与结果顺序也完全一致，区别仅在于把每条违规区间输出为一个 GeoJSON Feature：

- `entry`：几何为 `LineString`，坐标依次为**进入点 → 区间跨段途经的所有航迹顶点 → 离开点**（单航段时只有进入点和离开点）；
- `graze`：进入点与离开点重合时为 `Point`（如掠过顶点），否则为从进入点到离开点的 `LineString`（如沿边界滑行）。

所有坐标均为 `[经度, 纬度, 高度]`。每个 Feature 的 `properties` 携带 `zone_id`、`kind`、`entry_time`、`exit_time`、`segment_start`、`segment_end`；`features` 顺序与 `/audit` 的 `violations` 一致。

**响应 200**（以上面 /audit 的例子为例）

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": {
        "type": "LineString",
        "coordinates": [
          [116.30, 39.905, 50.0],
          [116.32, 39.905, 50.0]
        ]
      },
      "properties": {
        "zone_id": "nfz-1",
        "kind": "entry",
        "entry_time": "2026-09-22T10:06:00Z",
        "exit_time": "2026-09-22T10:18:00Z",
        "segment_start": 0,
        "segment_end": 0
      }
    }
  ]
}
```

无违规时返回 `{"type": "FeatureCollection", "features": []}`。422 校验规则与 `/audit` 相同。

### POST /audit/counterfactual

反事实高度审计：把全部航点高度统一平移 `altitude_offset` 米后重新执行与 `/audit` 完全相同的审计，再与原始（baseline）结果比对，回答“如果整体飞高/飞低 X 米，违规会如何变化”。

**请求体**在 `/audit` 的基础上增加一个必填字段：

- `altitude_offset`：高度平移量（米），正数整体抬高、负数整体降低；必须为有限数值（NaN/±Inf 返回 422）。

```json
{
  "waypoints": [
    {"lat": 39.905, "lon": 116.29, "alt": 50, "time": "2026-09-22T10:00:00Z"},
    {"lat": 39.905, "lon": 116.34, "alt": 50, "time": "2026-09-22T10:30:00Z"}
  ],
  "zones": [
    {
      "id": "nfz-1",
      "geometry": {
        "type": "Polygon",
        "coordinates": [[[116.30, 39.90], [116.32, 39.90], [116.32, 39.92], [116.30, 39.92], [116.30, 39.90]]]
      },
      "active_from": "2026-09-22T10:00:00Z",
      "active_to": "2026-09-22T11:00:00Z",
      "min_alt": 0,
      "max_alt": 120
    }
  ],
  "altitude_offset": 100
}
```

**响应 200**

```json
{
  "altitude_offset": 100.0,
  "baseline": {
    "violation_count": 1,
    "violations": [
      {
        "zone_id": "nfz-1",
        "kind": "entry",
        "entry_time": "2026-09-22T10:06:00Z",
        "exit_time": "2026-09-22T10:18:00Z",
        "entry_position": {"lat": 39.905, "lon": 116.30, "alt": 50.0},
        "exit_position": {"lat": 39.905, "lon": 116.32, "alt": 50.0},
        "segment_start": 0,
        "segment_end": 0
      }
    ]
  },
  "shifted": {"violation_count": 0, "violations": []},
  "cleared_count": 1,
  "introduced_count": 0,
  "cleared": [
    {
      "zone_id": "nfz-1",
      "kind": "entry",
      "entry_time": "2026-09-22T10:06:00Z",
      "exit_time": "2026-09-22T10:18:00Z",
      "entry_position": {"lat": 39.905, "lon": 116.30, "alt": 50.0},
      "exit_position": {"lat": 39.905, "lon": 116.32, "alt": 50.0},
      "segment_start": 0,
      "segment_end": 0
    }
  ],
  "introduced": []
}
```

- `baseline` / `shifted`：原始航迹与平移后航迹的两次完整审计结果，结构与 `/audit` 响应一致；`baseline` 与用同一请求体调用 `/audit` 的结果完全相同。
- `cleared` / `introduced`：两组违规摘要。两次结果按 **`(zone_id, entry_time, exit_time)`** 匹配（多重集语义）：基线中存在而平移后消失的记入 `cleared`，平移后才出现的记入 `introduced`。
- 注意平移会改变高度窗口的命中区间，幸存违规的进入/离开时刻可能随之改变；时刻发生变化的违规会呈现为一条 `cleared` 加一条 `introduced`。
- `cleared`、`introduced` 均按进入时间稳定排序，相同输入的响应逐字节一致。
- 平移在航点副本上进行，不修改请求中的原始数据。

**响应 422**：校验规则与 `/audit` 相同（航点乱序、多边形非法、参数错误等），此外 `altitude_offset` 缺失或不是有限数值也返回 422。

## 判定规则

对每一条**相邻航段**（而非仅航点）逐一审计。航段上任意点用分数 f ∈ [0,1] 表示，位置、时间、高度均为 f 的线性函数，因此三个条件各自对应一个 f 区间：

1. **平面命中**：Shapely 求线段与多边形的交，得到进入区间（交为线）或切点（交为点）；
2. **时段命中**：解 `t0 + f·Δt ∈ [active_from, active_to]`；
3. **高度命中**：解 `alt0 + f·Δalt ∈ [min_alt, max_alt]`。

三者交集非空才判定违规——即平面、时段、高度必须**同时**命中。交集为正长度且经过多边形内部记为 `entry`；退化为点或仅沿边界滑行记为 `graze`。跨航段首尾相接的命中会合并为一条连续违规区间（例如航迹在区内途经多个航点只产生一条记录）。

## 项目结构

```
app/
  main.py           # FastAPI 入口与路由（/audit、/audit/geojson、/audit/counterfactual）
  models.py         # 请求/响应模型与参数校验（含 GeoJSON、反事实响应模型）
  audit.py          # 核心审计算法（平面求交 + f 域三窗口求交 + 区间合并）
  geojson.py        # 违规区间 → GeoJSON FeatureCollection 转换
  counterfactual.py # 反事实审计（高度平移 + 两次审计结果按匹配键求差集）
tests/
  test_audit.py           # POST /audit
  test_geojson.py         # POST /audit/geojson
  test_counterfactual.py  # POST /audit/counterfactual
Dockerfile
docker-compose.yml
```
