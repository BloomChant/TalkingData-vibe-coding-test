# 契约变更记录与请求

流程：契约冻结后只加不改。
- 新增字段或接口：直接做，在这里记一笔。
- 改名、删除、改含义：先在这里提请求，由仓库主人确认。

## v1.0 基线（Claude 记录）
- 以 Codex `bcf8e9f` 已经实现的接口为基线；`fixtures/` 是从运行中的后端（run=`correctness-001`）录下来的。
- 自检：`node contracts/check.mjs http://localhost:8000 --run correctness-001`，基线全部符合。

## v1.1 请求（Claude → Codex，按优先级排列）

| 编号 | 内容 | 用途 | 状态 |
|---|---|---|---|
| R0 | `overview.series` 里没有数据的空桶，补齐 `run`、`latency_sum`、`latency_count`、`latency_max`（填 0），和有数据的桶结构一致。自检已经在 `throughput-max` 上发现这个不一致 | 前端按统一结构画速率和延迟 | 待实现 |
| R1 | `/api/objects` 增加 `kind`、`frame`、`frame_kind` 过滤参数（可重复）；返回 `total`、`total_exact`；`type_id` 允许重复 | 统一过滤，以及"命中 N 个" | 待实现 |
| R4 | `GET /api/reconcile?run=` 返回最近一次对账结果；对账结果里增加 `batches.states` | 对账页、批次像素条 | 待实现 |
| R2 | `GET /api/facets` | 每个过滤选项旁边的计数 | 待实现 |
| R3 | `GET /api/fields` | 字段目录，这是信息抽象的核心视图 | 待实现 |
| R5 | （可选）`frames`、`positions` 接受过滤参数；`quality` 增加 `excerpt` | 过滤后的空间视图；隔离区原文 | 可选 |

细节见 `API.md`。Codex 实现之前，前端对 🆕 接口做降级处理：隐藏计数，或者显示"后端尚未提供"，不会因此报错。

## 证据（供 Codex 判断，不是结论）

**探针 batch_id 和源批次的映射**：两轮数据显示同一个规律，`探针 batch_id = max_batches + 源批次号`。
- probe-e66cf7ae83（max_batches=300）：`fault_sent_bitmap` 的第 28、67 位，对应隔离批次 328、367。
- recovery-001（max_batches=20000，seed 42）：隔离批次是 20028、20067。同一个 seed 下，源批次同样是 28、67。

如果这个规律在 correctness-001 等轮次的回执上也成立，"隔离记录与探针逐条对应"这一项就可以从 `unknown` 变成可验证。
