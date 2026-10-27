# 接口契约 v1.1

前端（Claude）与后端（Codex）之间唯一的接口标准。

- **v1.0 基线**：Codex 已经实现的接口。`fixtures/` 里的示例是从运行中的后端（run=`correctness-001`）录下来的真实返回。
- **v1.1 新增**（标 🆕）：前端做"抽象 + 过滤"所需要的补充，**只加不改**，不影响已有字段。实现顺序见 `CHANGES.md`。
- 自检：`node contracts/check.mjs http://localhost:8000 [--run <id>]`。🆕 项没实现时只报 PENDING，不算错误。

## 通用约定
- JSON，UTF-8，字段名用 snake_case；同源访问，没有跨域问题。
- 时间单位：
  - **服务端时间**（`created`、`last_seen`、`second`、事件的 `created`）是 Unix **秒**，可以带小数。
  - 对象的 `timestamp_ms` 是发生器给的**生成时间**，单位毫秒。两者不能混用。
- `run` 参数可以不传，默认取 `last_seen` 最新的那一轮。`_unassigned` 是"无法归属轮次的拒收消息"，前端不把它当成普通轮次展示。
- 数据为空时返回合法的空结构；只有指定的对象或轮次不存在时才回 404。
- 数字字段可以是整数也可以是小数（例如 `rate`）。

## 前端自行派生的值（后端不用提供）
| 派生值 | 规则 |
|---|---|
| 对象种类 `kind` | `name` 去掉末尾的 `#数字`，正则 `/\s*#\d+$/`。界面上标"派生" |
| 顶层类目 | 在 `/api/tree` 里往上找，父节点是根（`parent=0`）的那一层。挂在根上的对象归为"万物（未细分）" |
| 类型路径 | 用 `/api/tree` 的 `parent` 链拼出来 |
| 未提供位置 | `overview.run.objects - overview.positioned` |
| 空间层级 | `frame_id` 按 `/` 切开，前缀就是上一级：星系 → 恒星系 → 世界 |

后端过滤参数里的 `kind` 必须使用**同一条规则**，否则前后端的计数会对不上。

## v1.0 基线（已实现）
| 接口 | 用途 | 示例 |
|---|---|---|
| `GET /api/runs` | 轮次列表：`{runs:[{id, created, last_seen, revision, objects, batches, duplicates, rejected, payload_bytes}]}` | `runs.json` |
| `GET /api/overview?run=` | 总览：`run` 行、`types`、`series`（最近 60 秒，每秒一桶，带 `latency_sum/count/max`）、`rate`、`active`、`operations`、`positioned` | `overview.json` |
| `GET /api/tree?run=` | 分类树：`{revision, nodes:[{id, parent, name, count, revision, subtree_count}]}` | `tree.json` |
| `GET /api/objects?run=&type_id=&q=&cursor=&limit=` | 对象分页。`type_id` 包含整棵子树；`q` 按名称子串匹配；`limit` 不超过 100 | `objects.json` |
| `GET /api/objects/{id}?run=` | 对象详情：`{object: 原样对象, tree_revision, type_name}` | `object_detail.json` |
| `GET /api/events?run=&limit=` | 事件（分类的新增、改名、迁移等），带 `summary` 和 `details` | `events.json` |
| `GET /api/quality?run=` | 拒收记录（最近 50 条）、重复次数、处理策略 | `quality.json` |
| `GET /api/system` | cgroup 资源（内存、CPU、数据量、磁盘水位、预算）；`null` 表示拿不到 | `system.json` |
| `GET /api/frames?run=` | 所有参考系：`{frames:[{frame_id, frame_kind, unit, count}], limit}` | `frames.json` |
| `GET /api/positions?run=&frame_id=&unit=&limit=` | 单个参考系内的点（取最新的若干个，不是无偏抽样） | `positions.json` |
| `POST /api/reconcile`（body 是回执 JSON） | 回执对账：`{run, ok, complete, checks:[{name, expected, actual, status, note?}], ...}` | `reconcile.json` |

## v1.1 新增 🆕

### R1 `GET /api/objects` 增加过滤参数和计数
- 新增参数，**可重复，同名参数之间是"或"，不同参数之间是"且"**：
  - `kind=<派生种类>`：规则同上；等价于 `name = kind OR name LIKE kind || ' #%'`。
  - `frame=<路径前缀>`：按路径段匹配，即 `frame_id = v OR frame_id LIKE v || '/%'`。
  - `frame_kind=galactic|stellar_system|local_surface|none`：`none` 表示 position 为 null。
- `type_id` 也允许重复（多选）。
- 返回里增加 `total`（满足条件的对象数）和 `total_exact`（布尔值；数据量大时可以返回估算值，同时置为 `false`）。

### R2 `GET /api/facets?run=&<R1 的全部过滤参数>&dims=type,kind,frame_kind,galaxy`
返回各个维度在当前过滤条件下的计数，示例见 `facets.json`：
```json
{"run": "…", "filters_applied": true, "total": 2940,
 "facets": {"type": {"buckets": [{"value": "17", "label": "远域星区 · 17", "count": 120}], "other": 0},
            "kind": {"buckets": [{"value": "监测终端", "label": "监测终端", "count": 423}], "other": 0},
            "frame_kind": {"buckets": [{"value": "none", "label": "未提供位置", "count": 428}], "other": 0},
            "galaxy": {"buckets": [{"value": "galaxy-0", "label": "galaxy-0", "count": 900}], "other": 0}}}
```
- `type` 维度返回**每个 type_id 的直属计数，不截断**，前端会沿分类树向上汇总。其他维度取前 100 个，剩下的合计放进 `other`。
- `galaxy` 取 `frame_id` 的第一段；没有位置的记为 `none`。
- 推荐采用标准 facet 语义：计算某个维度时，忽略该维度自身的过滤条件。如果暂时做不到，也可以全部条件都生效，但要在 `CHANGES.md` 里说明。
- 只能先部分实现时，返回 `filters_applied: false`。

### R3 `GET /api/fields?run=&<R1 的全部过滤参数>&sample=2000`
字段目录：在满足条件的**最新 N 个对象**上统计 `attributes` 里的字段路径，示例见 `fields.json`：
```json
{"run": "…", "filters_applied": true,
 "sample": {"size": 2000, "matched": 22200, "sampled": true, "order": "most_recent"},
 "fields": [{"path": "attributes.coordinates.ra_deg", "present": 812, "coverage": 0.406,
             "types": {"number": 800, "string": 12}, "mixed": true,
             "examples": [356.84], "numeric": {"min": 0.1, "max": 359.9}}]}
```
- 路径写法：用 `.` 分隔；数组元素记作 `[]`（例如 `attributes.tags[]`）；最多展开 4 层。
- `types` 的取值只有 `string`、`number`、`boolean`、`null`、`object`、`array`。
- `mixed` 表示除 null 之外出现了不止一种类型。
- `examples` 最多 3 个标量，字符串截断到 40 个字符。
- 按 `present` 从高到低排序，最多返回 200 条。

### R4 `GET /api/reconcile?run=`：取最近一次对账结果
- 返回该轮次最近一次 `POST /api/reconcile` 的结果（结构与 POST 的返回相同），另加 `checked_at`（秒）。
- 从来没对过账时返回 `{"run": "<id>", "available": false}`。
- POST 和 GET 的结果里都增加 `batches: {"size": <max_batches>, "states": "<每个批次一个字符>"}`，字符含义：
  - `A`：已确认且已入库
  - `M`：已确认但缺失
  - `U`：已入库但发生器没有确认
  - `X`：发送了，但既没确认也没入库
  - `-`：没有发送
  前端用这串字符画"批次像素条"。

### R5（可选）
- `frames`、`positions` 也接受 R1 的过滤参数。
- `quality` 的每条记录增加 `excerpt`：引起拒收的那部分原文，最多 1 KB。
