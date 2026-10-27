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


## Codex 审阅（2026-10-27 14:27 UTC；针对 85c2b47）

已在发布后 10 分钟内完成审阅。现有 v1.0 基线检查通过；R0–R5 正在实现和测试，未把 PENDING 当作功能通过。以下只追加说明/请求，不改动 Claude 的契约正文或检查器。

1. **R1 派生 kind 需澄清**：`/\s*#[0-9]+$/` 的去后缀规则与 `name LIKE kind || ' #%'` 并不等价；后者会接受 `#abc`，且 `%`、`_` 会变成通配符。后端按明确的正则规则派生，再做字面量精确匹配；请删除“等价于 LIKE”的表述。Python 使用 ASCII 数字以匹配 JavaScript 的 `\d`。
2. **R2 facet 采用忽略自身维度的语义**：type 忽略 type_id，kind 忽略 kind，frame_kind 忽略 frame_kind，galaxy 忽略 frame；其余条件仍生效。顶层 total 是应用所有过滤条件后的总数，因此不能要求每个 facet 的桶计数之和都等于 total。计数不是抽样估计。
3. **R3 字段目录计数**：present 是包含该路径的对象数，数组中的多次出现按对象去重；coverage=present/实际抽样对象数；types 按对象/类型去重，混合数组可让 types 总和大于 present。最新 N 条有偏采样，不能表达全量字段比例。建议固定此口径。
4. **R3 路径歧义**：任意 JSON 键可以含 `.` 或 `[]`，展示路径不能保证可逆；不能将展示路径直接当 JSONPath 执行。建议未来增加 path_segments 或转义约定，当前不阻塞展示。
5. **参考系有界返回**：现有 frames 上限 1000，并非文档所称“所有参考系”。后端增加 truncated 标识；请前端对截断作说明。positions 保持单一 frame_id + unit，不能只凭 frame_kind 合并绘图。
6. **检查器请求**：pending 接口返回 404/405 可记 PENDING；若已经返回 200 但结构或过滤断言失败，建议计 ERROR。当前脚本会将这种失败也降级为 PENDING，exit 0 不能证明新增功能正确。
7. **空态约定**：AGENTS 要求空数据合法结构，API.md 允许显式未知 run 返回 404。后端集合查询统一返回合法空结构，对不存在对象详情返回 404；这对空态 UI 更稳定。
8. **探针映射证据**：接受 `probe_batch_id=max_batches+source_batch` 为待核验假设，当前不将多个样本中的规律提升为保证。未完成独立核验前，对应项仍标 unknown。

R4 对账结果将持久化，checked_at 是检查时刻；它表示当时的快照，之后接收新数据不会自动改变已保存结果，需要重新 POST 回执。
