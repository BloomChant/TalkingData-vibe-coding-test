# fixtures

- **v1.0 基线**：runs、overview、tree、objects、object_detail、events、quality、system、frames、positions、reconcile。
  - 这些是从运行中的后端（Codex `bcf8e9f`，run=`correctness-001`）录下来的。
  - `reconcile.json` 取自 `results/correctness-001/reconcile.json`。
- **v1.1 新增**：facets、fields。
  - 后端还没有实现，由 Claude 用 30 秒真实采样（probe-e66cf7ae83）计算得到，结构就是要请求的契约。
  - 采样按我们的接收规则重放过：拒收 2 个探针、吸收 4 次重复，最终 2940 个对象。
- 示例只用来约定结构，里面的数值不作为验收标准。
