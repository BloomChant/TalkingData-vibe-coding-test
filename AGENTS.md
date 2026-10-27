# AGENTS.md：协作规则（Codex 必读）

本仓库由两个 AI 协作完成：**Codex 负责后端**，**Claude 负责前端**，仓库主人负责在两边之间传话和拍板。
背景见 `PLAN.md`。**请先读顶部的勘误**；协议细节以 `docs/protocol.md` 和实测数据为准。

## 分工与目录归属（只改自己的目录）

| 目录 / 文件 | 归属 |
|---|---|
| `server/`、`scripts/`、`samples/`、`docs/protocol.md`、`Dockerfile`、`compose.yaml`、`README.md`、`results/` | Codex |
| `web/`（包括提交进仓库的构建产物 `web/dist`）、`docs/frontend.md` | Claude |
| `contracts/` | 共有：Claude 起草和维护；Codex 通过 `contracts/CHANGES.md` 提修改请求 |
| `PLAN.md`、`AGENTS.md`、`CLAUDE.md`、`.gitignore` | 共有：只追加，不删改对方写的内容 |

后端技术栈由 Codex 决定（当前骨架是 Python）。契约与实现语言无关。

## 接口契约（`contracts/`）
- `contracts/API.md` 是唯一标准。`contracts/fixtures/*.json` 是每个接口的示例返回，由真实采样生成。
- 后端返回的结构必须和示例一致：可以多字段，不能少字段，也不能改字段类型。
- 每次 push 前运行 `node contracts/check.mjs http://localhost:8000`，必须零错误。
- v1 推送后大约 10 分钟冻结。冻结之后**只加不改**：
  - 新增字段或接口：直接做，在 `CHANGES.md` 里记一笔。
  - 改名、删除、改含义：先在 `CHANGES.md` 提请求，等仓库主人确认。
- 契约和真实数据冲突时，在 `CHANGES.md` 里写清楚；接口先返回合法的占位值（null / [] / 0），不要报错。
- 接收统计的口径要分开，不能混用：收到、原文已保存、接受（2xx）、拒收（4xx，按原因分）、重复（同一个键、内容摘要相同）、冲突（同一个键、内容摘要不同）。

## 服务约定
- 统一用端口 8000：
  - `/`：前端页面，静态文件来自 `web/dist`，未知路径回退到 `index.html`
  - `/api/*`：查询接口
  - `/ingest`：接收发生器数据
- `web/dist` 由 Claude 构建并提交。Dockerfile 只负责复制它，**不要在 Docker 里运行 npm**。
- 数据为空时也要返回合法结构，不要返回 404 或 500。

## Git 规则
- 本地副本：Codex 用 `/Users/apple/TalkingData-vibe-coding-test`，Claude 用 `/Users/apple/TalkingData-vibe-coding-test-frontend`。
- **唯一的主项目目录就是这个仓库。** `~/Documents/Codex/.../outputs/observatory` 里的骨架迁移进 `server/` 之后就不再使用。
- 提交身份在仓库内设置：
  ```
  git config user.name BloomChant
  git config user.email 71309266+BloomChant@users.noreply.github.com
  ```
- 只 `git add` 自己的目录，**禁止 `git add -A` 和 `git add .`**。
- push 前先 `git pull --rebase`；遇到网络 SSL 报错就重试。
- commit 前缀：`server:`、`web:`、`contract:`、`docs:`、`test:`。

## 节奏
- 每到一档都要 push：T0 骨架 → T1 MVP → T2 增强 → 冻结 → 提交。具体时间按实际剩余时间排（见 PLAN 第 8 节）。
- **Codex 接下来要做的**：
  1. 把骨架迁移进 `server/`。
  2. 把真实采样的小样放进 `samples/`：几条 types 消息、几条 objects 消息、两条坏探针、receipt、probe-report。
  3. 写 `docs/protocol.md`，然后 push。
  Claude 会根据这些小样生成契约的示例数据。

## 本机环境
- 发题包：`/Users/apple/universe-observatory/task-package/`
- docker：`/Applications/Docker.app/Contents/Resources/bin/docker`（可能不在 PATH 里）
- Docker 虚拟机：8 CPU / 7.75 GiB。我们的容器合计限制在 4 CPU / 4 GiB；默认 compose 里不要写 `cpuset`。
