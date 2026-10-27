# 宇宙万物观测站

**所选题目：题目二。** 一个用于理解持续流入的陌生对象的单机观测工作台：接收出题方发生器的真实 HTTP 消息，维护演化中的分类树，检索对象及动态属性，并用回执核对接收结果。

后端使用 **Python / FastAPI + SQLite**，前端由 Claude 独立开发，构建产物提交在 `web/dist`。Docker 镜像直接复制前端构建产物，不运行 npm；页面与 API 由同一后端进程提供。后台全部运行在一个 Linux arm64 应用容器中；发生器单独运行。

## 快速运行

环境：Mac 上的 Docker Desktop，支持 Linux arm64。建议 Docker 虚拟机至少有 6 CPU、6 GiB 内存，为应用、发生器及虚拟机开销留空间；应用容器仍严格限制为 4 CPU / 4 GiB。

在仓库根目录执行：

```sh
docker compose up -d --build
curl -fsS http://127.0.0.1:8000/health
```

打开 **http://127.0.0.1:8000**。没有数据时页面显示空状态；运行下面的官方发生器测试后即可探索真实对象。

### 准备官方发生器

镜像随题目附件提供，**不要假定 Docker Hub 上存在可拉取的公开镜像**。

1. 从[发题附件](https://github.com/TD-ding/space/blob/main/%E5%AE%87%E5%AE%99%E4%B8%87%E7%89%A9%E8%A7%82%E6%B5%8B%E7%AB%99%E5%80%99%E9%80%89%E4%BA%BA%E5%8F%91%E9%A2%98%E5%8C%85.zip)下载 ZIP，解压到仓库的 `task-package/`。
2. 在解压出的 `宇宙万物观测站` 目录执行 `shasum -a 256 -c SHA256SUMS`。
3. 导入镜像，再启动发生器：

```sh
docker load -i 'task-package/宇宙万物观测站/flightgen-image.tar'
docker compose -f compose.yaml -f compose.generator.yaml up -d --build
curl -fsS http://127.0.0.1:9001/status
```

如已经按出题方说明启动了发生器，保留它即可，不要再启动第二个占用 9001 端口的容器。`compose.generator.yaml` 将发生器限制为 1 CPU、256 MiB；它不计入应用的 4 CPU / 4 GiB 预算。

### 一键实际测试与对账

测试脚本仅依赖宿主机 Python 3 标准库：

```sh
python3 scripts/run-test.py --rate 200 --duration 120 --batch-size 20 --workers 4
```

脚本会生成新的 `test_id`，启动指定时长的一轮，等待发生器结束，保存回执并调用本系统对账。证据存入 `results/<test_id>/`：

- `request-config.json` / `start.json`：请求参数及发生器实际接受的配置；
- `status.json` / `receipt.json`：发生器原始最终状态和回执；
- `reconcile.json`：逐项对账结果，区分 `pass`、`fail`、`unknown`；
- `overview.json` / `system.json` / `summary.json`：应用统计、资源快照、实测概要。

默认发送路径为 `host.docker.internal:8000/ingest`，与附件说明一致。可通过 `--generator`、`--app`、`--target` 覆盖地址。速率 **0 表示尽力发送，绝非暂停**。重复执行时请使用新的 `test_id`。

```sh
# 温和的阶梯测试，先完成正确性检查再提升负载
python3 scripts/run-test.py --rate 1000 --duration 60 --batch-size 100 --workers 4 --profile steady

# 对已有回执重新核对，例如重启应用后
python3 scripts/reconcile.py results/<test_id>/receipt.json
```

脚本退出码：0 = 已实现的检查没有失败；2 = 对账存在失败；1 = 流程或网络异常。**0 不代表所有潜在检查都完成**：`complete=false` 表示仍有 `unknown` 项。

## 页面使用

1. 选择运行轮次，查看对象数、分类数、接收速率、重复与异常统计。
2. 展开分类树；选择一个节点，查询其自身及子树里的对象。
3. 通过中文名称搜索，按游标翻页。选择对象，查看动态属性、坐标和完整 JSON。
4. 查看分类新增、改名、迁移事件；不会把分类移动描述成对象的物理运动。
5. 数据质量区域列出拒绝批次的原因；上传发生器回执可展示核对结果。
6. 系统区域读取容器实际 cgroup 指标，展示 CPU、内存及数据目录占用。非 Linux 容器环境缺少 cgroup 时指标显示不可用，不生成假数值。

空间数据按明确的 `frame_id` 和单位分别查询。没有位置的对象显示“未提供位置”；不会据此声称对象失联。空间接口最多返回 2,000 个最新对象，是有界样本，不代表完整空间分布。

## 架构与数据正确性

```text
官方发生器
    │ HTTP POST /ingest
    ▼
大小限制 → JSON / 公共结构校验 → 等待单写锁（有界等待）
    │
    ├─ 异常：整批拒绝，保存压缩原文到隔离表，返回 400 / 409 / 422
    │
    └─ 有效：SQLite 事务
          ├─ 批次幂等记录 + 压缩原始请求
          ├─ 分类操作 / 对象记录 / 计数
          └─ 提交成功后返回 2xx
                    │
           查询 API / 每秒统计桶
                    │
           同源静态前端，定时刷新
```

- SQLite 使用 `journal_mode=WAL`、`synchronous=FULL`。一个 HTTP 批次内的业务记录和幂等记录原子提交；不会 ACK 后仅保存在内存队列。
- 幂等键来自实际消息体：`(test_id, kind, batch_id)`；分类消息使用 `(test_id, kind, revision)`。对规范化 JSON 计算 SHA-256，同键同内容返回 200，同键不同内容返回 409。原始请求以 zlib 压缩保存，解压后保留原字节。
- 同一对象 ID 出现在不同批次中时，相同内容不重复计数；不同内容返回 409 并整批隔离。**已观测协议没有对象版本字段，因此不编造更新顺序或用到达时间覆盖数据。**
- 分类按连续 revision 应用。重复版本幂等；版本缺口返回 503，尚未到达的分类依赖不会被悄悄忽略。分类迁移检测环，非法操作整条事务回滚。
- `attributes` 中允许未知字段、混合类型、嵌套结构和 null。公共标识、分类关联和坐标结构单独校验。故障头部只保留作诊断，不用于跳过内容校验。
- 各轮次按 `test_id` 隔离；所有真值和统计桶在 SQLite 中，重启后无需依赖丢失的内存状态。
- 当前每个请求一个业务事务；尚未实现跨请求 group commit。应用使用单 Uvicorn worker，写锁串行化 SQLite 写入。

## 资源控制

| 资源 | 应用容器配置 | 验证方法 |
|---|---|---|
| CPU | `cpus: 4.0` | `docker inspect`，以及 cgroup `cpu.max` |
| 内存 | `mem_limit: 4g` | `docker stats`，以及 cgroup `memory.max` |
| 内存 + swap | `memswap_limit: 4g` | 禁止靠额外 swap 超出预算 |
| 数据文件 | `/data` 命名卷 | 数据库、WAL 等全部计入 `data_bytes` |
| 容量准入 | 16 GiB 告警，接近 18 GiB 拒收 | 预留空间，返回 507，不返回成功 |
| 日志 | `local` 驱动，10 MiB × 3 | Docker 单独轮转日志，约 30 MiB 加管理开销 |
| 临时文件 | 64 MiB tmpfs | 容器根文件系统只读；临时盘属于内存预算 |
| 请求 | 最大 8 MiB，连接并发上限 32 | 超大请求 413；写锁拥塞 429 + Retry-After |

**20 GiB 是整体持久化预算，不是本项目已配置的文件系统硬配额。** 普通 Docker named volume 没有自动 20 GiB 限制。当前通过单写入口检查数据占用、保守预估写放大、18 GiB 水位和日志轮转保护预算；这不能替代接近容量边界时的真实验证。Docker 镜像和构建缓存不写入应用数据卷。

```sh
docker compose ps
docker stats --no-stream
docker inspect "$(docker compose ps -q app)" \
  --format 'CPU={{.HostConfig.NanoCpus}} memory={{.HostConfig.Memory}} swapTotal={{.HostConfig.MemorySwap}}'
curl -fsS http://127.0.0.1:8000/api/system
```

`cpu_percent` 中 100% 表示一个 CPU 核，所以四核预算对应最多约 400%。这不是“占四核预算的百分比”。

## 复现与验收

### 自动测试

本地开发需要 Python 3.12 或更高版本：

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r server/requirements-dev.txt
python -m pytest server/tests -q
```

测试覆盖：事务提交与原始请求保真、并发重复幂等、同键冲突、整批回滚、动态属性、分类改名与迁移、环检测、版本缺口、轮次隔离、容量拒收、请求大小限制、非法 JSON、数值/Unicode 边界、应用重启后的持久化。

### 重启恢复

先完成一轮实际运行并保存回执，再执行：

```sh
docker compose restart app
curl -fsS http://127.0.0.1:8000/health
python3 scripts/reconcile.py results/<test_id>/receipt.json
```

这验证正常重启。**进程强杀恢复应单独实测**：运行过程中终止应用，重启后等待发生器完成，再核对已 ACK 的批次是否全部存在；不能把正常重启测试称为断电测试。

### 实际测试结果

已完成的自动测试和现场运行证据应以仓库中的 `results/` 原始文件为准。当前功能级测试验证 20 项接收与恢复场景全部通过；测试数量会随查询与对账测试补充而增加。

开发前的协议侦察以目标 100 对象/秒运行 30 秒，保存 183 条 HTTP 消息、4,491,104 字节，与发生器回执一致。随后对这些消息做应用逻辑回放：177 条新接收（30 条分类消息、147 个正常对象批次）、4 次重复、2 个异常批次返回 422，正常对象 2,940 个，分类版本 30。**这组是协议/逻辑验证，不是当前容器的性能上限。**

容器内官方发生器正确性测试已完成，证据见 [`results/correctness-001/`](results/correctness-001/)：

| 指标 | 实测值 |
|---|---:|
| 配置 | evolving、bursty、目标 200 对象/秒、batch=20、workers=4 |
| 实际持续时间 | 120.005 秒 |
| 已确认主批次 | 1,110 |
| 已确认主流对象 | 22,200 |
| 实际平均确认速率 | 184.992 对象/秒 |
| 发生器主请求平均 / 最大延迟 | 14.417 / 166.554 ms |
| 已确认批次缺失 | 0 |
| 分类确认版本 / 本地版本 | 112 / 112 |
| 异常探针确认 / 拒收源批次 | 0 / 18 |
| 重复确认 / 本地吸收 | 28 / 28 |
| 对账 | `ok=true`、`complete=false`（2 项 unknown） |

这是低速正确性运行，**不是吞吐上限测试**。未知项包括故障探针逐条身份映射和独立对象内容核验；没有把未知项算成通过。

不要把发生器设定速率当作实际吞吐。`run-test.py` 用已确认的主流对象数除以运行时间计算平均实际吞吐，并单独记录发生器请求延迟；不虚构 p95/p99。

## 回执对账的含义

运行回执提供 `sent_bitmap`、`ack_bitmap`（`lsb0`）、确认批次/对象数、分类确认版本，以及重复和故障注入 bitmap。系统核对：

- 回执已确认的批次全部存在本地；
- 已确认主批次对象记录数与发生器主流计数一致；
- 对象行数、分类直属计数、持久化汇总一致；
- 分类版本连续，已确认分类版本已保存；
- 故障探针成功确认数为零（本系统的结构校验策略）；
- 本地重复吸收数量不低于回执中的重复确认数。

“已入库但发生器未收到确认”可能发生于提交后响应丢失，不能当作已确认数据丢失。运行回执**不包含**逐对象完整内容、类型频率或字段结构统计；这些字段属于发生器的离线 `inspect` 输出。逐对象内容校验，以及 source-batch 故障 bitmap 到具体探针 ID 的映射尚未建立，明确显示 `unknown`，不会包装成完整验收通过。

## 目录结构

```text
server/app/
  main.py          HTTP 接收入口、静态页面、异常处理
  protocol.py      从真实样本归纳的公共结构校验
  storage.py       SQLite schema、事务、幂等、分类和对象写入
  queries.py       查询、统计、资源指标
  reconcile.py     回执 bitmap 核对
web/dist/          前端已构建产物（Claude 维护）
contracts/         前后端接口契约与检查脚本
scripts/           有界运行、回执获取与对账脚本
server/tests/      正确性与恢复测试
docs/protocol.md   真实协议与未知项
results/           实测配置、回执、核对及指标
compose.yaml       应用资源约束
compose.generator.yaml  可选的独立发生器
Dockerfile         Linux arm64 应用镜像
```

## 本地运行后端

完成依赖安装后：

```sh
DATA_DIR=./data STATIC_DIR=./web/dist uvicorn server.app.main:app --host 0.0.0.0 --port 8000 --workers 1 --limit-concurrency 32
```

浏览器访问本机 8000；发生器容器访问 `host.docker.internal:8000/ingest`。本地 Python 进程不受 Compose 的资源限制，因此正式性能结果必须在容器内取得。

## 已知限制与关键取舍

- 当前是本机验收工具，无账号和权限系统。Compose 只将端口发布到 `127.0.0.1`，不适合直接公开到互联网。
- SQLite 单写线程优先保证事务和恢复清晰；高吞吐下可能出现写锁背压。尚无跨请求 group commit、数据库分片或长期归档策略。
- 中文名称查询是参数化子串匹配，不是全文检索引擎；数据量很大时搜索和分类/空间聚合查询会变慢。
- SQLite `FULL` 提供数据库持久性保障，最终仍受 Docker Desktop、宿主机存储和硬件行为约束。
- 容量保护、日志轮转和接近满盘的真实恢复测试应分别验证；未声称已达到 20 GiB 极限规模。
- 对象属性可扩展；分类来自实时树，未把“星体/生物/航行器/外星智慧”四类硬编码成协议真相。
- 前端轮询刷新，不是逐事件流式推送；空间样本有明确上限，不画所有对象，不把不同 frame/unit 混合。
- 无对象版本字段时不提供虚构的历史更新、轨道关系或信号丢失叙事。
- AI 参与编码、测试和文档；提交者仍需理解全部设计与取舍。

停止应用：`docker compose down`。数据卷会保留；只有明确要清空所有数据时才使用 `docker compose down -v`。

## 协作与接口契约

`contracts/API.md` 和 `contracts/fixtures` 是前后端联调标准；后端由 Codex 维护，`web/` 与前端构建产物由 Claude 维护。提交前在服务运行时执行 `node contracts/check.mjs http://localhost:8000`。契约检查通过不替代数据正确性和资源压测。
