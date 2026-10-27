# CLAUDE.md：Claude（前端）协作规则

完整规则见 `AGENTS.md`。要点如下：

- 只改 `web/`、`contracts/`、`docs/frontend.md`；只 `git add` 自己的目录；push 前先 `git pull --rebase`。
- 契约冻结后只加不改，所有改动记入 `contracts/CHANGES.md`。
- 先用 `contracts/fixtures`（由真实采样生成）开发；联调时让 Vite 把 `/api` 代理到 `http://localhost:8000`。
- 每次改完前端都运行 `npm run build`，把 `web/dist` 一起提交。
- 数据语义边界：不编造协议里没有的关系和事件。
  - 对象没有版本
  - 协议里没有 family 字段
  - `position` 为 null 时显示"未提供位置"
  - 不同参考系的对象不能画在同一张坐标图上
  - 派生出来的标签必须注明"派生"
- 设计重心：
  - 信息抽象：分类树、对象种类、空间层级、字段目录、接收质量
  - 统一过滤：一条全局过滤条；每个视图同时也是过滤器；每个选项旁显示 facet 计数
