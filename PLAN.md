# 宇宙万物观测站：情报汇总 + 实施计划（前后端共用）

> 来源：题目原文、《发生器操作说明》，以及对发生器镜像里 Go 二进制的**静态分析**（strings / 符号表 / struct tag），**尚未实际运行**。
> 标【确认】的内容直接来自文档或二进制字面量；标【推断】的内容需要在第 2 节的侦察步骤里抓包核实。


> **⚠️ 勘误（以实测为准，优先于下文）**：第 1 节的协议推断来自对二进制的静态分析，其中多处已被 Codex 的真实采样推翻。**一切以 `docs/protocol.md`（Codex 维护）和下面的实测事实为准**，下文与之冲突的段落一律作废。
>
> 实测事实（来源：30 秒真实采样 `probe-e66cf7ae83`，共 183 条消息、2980 个唯一对象）：
> 1. **消息只有两种**：
>    - `{"kind":"types","test_id","revision","changes":[{"op":"create|rename|move","node":{"type_id","parent_id","name"}}]}`
>    - `{"kind":"objects","test_id","batch_id","tree_revision","objects":[…]}`
> 2. **对象字段只有** `id, sequence, type_id, name, timestamp_ms, position, attributes`。
>    - 没有 version / revision。同一个 id 从未出现过不同的内容；重复投递是整条消息字节级相同。
>    - 因此不做"按最高版本覆盖"，也不做对象版本时间线；`sequence` 也不能当版本号用。
> 3. **分类树**：根节点"万物"下有 11 个顶层类目：设备、文档、商品、样本、任务、观测、资产、服务、星体、生物、航行器。30 个 revision 共产生 66 个节点，最深 4 层；外星生命挂在"生物"下面。
>    - 协议里**没有 family 字段**，"四大家族"的说法作废。
>    - 以真实分类树为主；任何派生出来的标签都必须注明"派生"。
> 4. **位置**：要么是 `{"frame_id","frame_kind","unit","x","y","z"}`，要么是 null。`frame_kind` 有三种：
>    - galactic：单位 ly，`frame_id` 形如 `galaxy-1`
>    - stellar_system：单位 km，形如 `galaxy-0/system-3`
>    - local_surface：单位 m，形如 `galaxy-0/system-0/world-1`
>    - `frame_id` 是**包含路径**（星系 → 恒星系 → 世界），可以用来做空间层级导航；但**不同参考系的对象不能画在同一张坐标图上**。
>    - null 主要出现在文档、任务、服务上，界面显示"未提供位置"。**不做"信号丢失 / 观测受阻"叙事**，4.1 节里的相关内容作废。原文写的"采纳 Codex 的判断"有误：Codex 只确认过"位置可能为空"。
> 5. 协议里**没有对象之间的归属字段**（比如某颗卫星属于哪颗行星），4.1 节的 L3"天体系统"作废。
> 6. **坏数据探针**：incomplete = 缺少 `type_id`；invalid = `attributes` 是数组而不是对象。
>    - "必须拒收"并不是已知的官方规则，而是**我们自己定义的结构校验规则**：不合格的整条消息回 4xx，原文进入隔离区，不进入正常业务表。
> 7. **receipt 里只有这些**：批次的 sent / ack bitmap（base64，lsb0）、重复和故障投递的 sent / ack / rejected bitmap、分类树的 attempted / acknowledged revision、HTTP 统计、配置回显。
>    - **没有**各类型数量、最终的树节点、字段统计。这些属于离线 `inspect` 的输出，不能直接拿来和运行结果比总数。
>    - 因此对账分成两类："运行回执对账"（批次级）和"离线参考核验"（可选，必须注明统计口径）。
> 8. "每秒 2 万条"只是一个测试档位，不是验收的最低要求。"压缩 4–8 倍""几十万个点不卡"都还是待测假设。
>
> **范围（以此为准，取代第 4 节和第 8 节里不一致的优先级）**：
> - **必须完成**：可靠接收、结构校验、幂等、分类演化、对象的分页 / 过滤 / 详情、接收统计、回执对账、Docker、README。
> - **闭环后再加**：空间层级导航，加上单一参考系内的二维分布；批量提交；吞吐和恢复测试。
> - **暂缓**：天体归属、信号丢失叙事、版本 diff、分类历史回放、复杂的结构分析。

---

## 0. 一句话理解题目

发生器不只是数据源，它同时也是**考官**。它负责造数据：四大家族对象，加上演化中不断冒出来的新类型和一棵不断增删、改名、迁移的分类树。它还会故意制造**重复投递、坏数据探针、突发流量**，并在 `receipt` 里记下"标准答案"：每个批次有没有被确认、坏数据有没有被拒收、最终分类树长什么样、各类型数量和字段结构统计。

所以要做的"探测站"包含四件事：
**① 可靠接收（不丢、不重、不收坏数据）→ ② 理解并组织（分类、结构、版本）→ ③ 可视化"有什么 / 正在发生什么" → ④ 拿 receipt 自证数据可信。**

只做"接收 + 前端展示"会丢掉评分里"数据正确性、性能、可靠性"那几项。

---

## 1. 已掌握的事实

### 1.1 发题包
- 位置：`/Users/apple/universe-observatory/task-package/`（已从 github.com/TD-ding/space 下载，SHA256SUMS 校验全部 OK）
- 镜像：`talkingdata/flightgen:exam-v4-f1`，linux/arm64，单层，内容是 `/flightgen`（Go 1.25.6 静态二进制，环境变量 `GOMAXPROCS=1`、`GOMEMLIMIT=192MiB`）【确认】
- 默认 CMD：`serve --listen 0.0.0.0:9001 --output /runs --duplicate-rate 0.02 --incomplete-rate 0.01 --invalid-rate 0.01`【确认】
- 子命令：`run | serve | replay | preview | inspect`【确认】
  - `preview`：离线打印样例对象（"objects to preview (1..1000)"），也能打印指定 revision 的分类树（"tree revision to preview (1..1000)"）
  - `inspect`：统计前 N 个对象的结构（"number of objects sampled from the beginning (1..1000000)"）
  - `replay --manifest ...`：重放某个批次
- **本机目前没有安装 Docker**，这是第一个阻塞项。

### 1.2 控制面（:9001）【确认】
| 接口 | 说明 |
|---|---|
| `GET /health` | 健康检查 |
| `POST /start` | 启动一轮；运行中再次调用返回 409 |
| `GET /status` | 外层 `state`；字段包括 current/target_objects_per_second、ramp_progress、in_flight、http_2xx/429/4xx/5xx、timeouts、average/max_latency_ms、attempted/acknowledged_batches/objects 等 |
| `POST /rate` | `{"objects_per_second":N}`；`0` 表示尽力发送，不是暂停 |
| `POST /stop` | 返回 202，收尾在后台完成 |
| `GET /receipt` | `state=finished` 后可取；之前返回 409 |

`/start` 参数：`workload`(objects)、`target_url`、`test_id`（1–48 位 `[A-Za-z0-9_-]`，每轮换一个新的）、`seed`、`batch_size`、`workers`、`objects_per_second`、`rate_profile`(`steady`|`bursty`)、`ramp_seconds`、`duration_seconds`、`max_batches`、`retries`(0..5)、`timeout_ms`、`rate_stages`（例：`[{"after_seconds":5,"objects_per_second":10000}]`）、`objects{scenario: mixed|evolving|stress, tree_every_batches, initial_types/max_types (16 ≤ initial ≤ max ≤ 8192), max_depth, ...}`

### 1.3 数据面（发生器 POST 到我们的 target_url）
- `Content-Type: application/json`【确认】
- 二进制里出现的请求头【确认存在，具体用法待抓包】：`X-Flightgen-Test-Id`、`X-Flightgen-Batch-Id`、`X-Flightgen-Kind`、`X-Flightgen-Tree-Revision`、`X-Idempotency-Key`（另有 `Idempotency-Key`）
- 契约【确认】：**2xx = 整条消息已可靠保存**；非 2xx 或超时 = 未确认，可能重试；"部分发送依赖前序确认"
- 发生器能识别 `429 + Retry-After`（单独计数 `http_429`），可以拿来做背压【确认】
- 发生器会检测请求体有没有被完整读取（`guardedRequestBody`、`response_body_errors`、`interrupted_requests`），所以**必须读完 body 再回复**【确认存在；具体判定标准属推断】
- 消息种类：
  1. **对象批次**：每批 `batch_size` 个对象【推断：信封结构待抓包】
  2. **分类树更新**：每 `tree_every_batches` 批发一次，内容是 TypeTree 的 `add / move / rename` 节点操作，带 `revision`；推测下一个 revision 要等上一个被 ACK 后才发（对应"依赖前序确认"）【函数名确认，格式推断】
  3. **重复投递**：flag 说明原文是 *"probability of one additional delivery after a normal batch ACK"*，也就是**已经 ACK 的批次会再来一次**，应当是同一个幂等键【确认】
  4. **故障探针**：*"probability of an invalid / incomplete object probe per source batch"*，即额外发一个含非法或残缺对象的请求。receipt 里有 `fault_sent / fault_ack / fault_rejected` 三个 bitmap，所以**期望行为是拒收（4xx），而且不能入库**【说明文字确认，期望行为推断】

### 1.4 对象内容（字段词表来自二进制字面量）
| 家族 | 代表字段 / 取值 |
|---|---|
| 星体 celestial | `body_kind`: star/planet/moon/asteroid/comet/nebula/black_hole/gas_giant/rocky_planet；`mass_solar`、`luminosity_solar`、`spectral_class`、`surface_temperature_k`、`radius_km`、`orbit`、`semi_major_axis_au`、`atmosphere.dominant_gas`、`rings`、`crater_count`、`tidally_locked`、`region.extent_ly` |
| 生物 biological | `organism_kind`、Animalia/Bacteria、`habitat`: grassland/wetland/forest/coastal/soil、herbivore/carnivore/omnivore、`growth_phase`、`limb_count`、`eye_count`、`sampling_site`、`preservation` |
| 航行器 vessel | spacecraft/aircraft/submersible、`flight_state`、`engine_count`、`mission_days`、`cargo_capacity_t`、`passenger_capacity`、`home_base`、`registration`、`manufacturer`、`maintenance_records`、`firmware` |
| 外星智慧 alien_intelligence | `colony`、`colony_age_hours`、`communication.mode`: rhythmic-vibration/resonant-light/spoken-symbolic、`pulse_pattern`、`senses`、`morphology`: humanoid/multiped/silicate/metallic、`taxonomy`、`identification_confidence` |
| 未知 / 新类型 | `"group":"synthetic-…"`、`"provisional":true`（evolving 场景里不断出现） |

- **层级关系**【部分确认】：
  - 一批消息以 `{"kind":"objects","test_id":"…` 开头。这里的 `kind` 是**消息类型**，不是对象的大类。
  - 每个对象以 `{"id":"…` 开头，带 `type_id`，指向分类树上的一个类型节点。
  - **大类是类型的属性**：程序里有报错 `type %d changed family`，说明一个类型的大类不允许改变，分类树再怎么改名、迁移，大类都不变。
  - 所以对象的大类 = 它的类型所属的大类，要用 type_id 查分类树得到，或者从大类专属字段推断。具体以抓包为准。
- **程序里的中文类型名**（大类归属按语义推断）：
  - 星体：恒星、彗星、黑洞、星云、小行星、岩质行星、天然卫星、气态巨行星
  - 生物：鸟类、植物、陆生动物、微生物菌落
  - 航行器：飞机、船舶、潜航器、航天器
  - 外星智慧：拟人外星访客、多足外星生物、外星群体意识、外星晶体生命
  - 其他"万物"：设备、文档、商品、样本、任务、观测、资产、服务，以及监测终端、土壤标本、冷链终端、边缘网关、通信链路等
  - 演化中出现的新类型，很可能是"基础类型 + 修饰词"。修饰词有工业级、便携型、高亮度组、长航程组、巡航组、常见种群组等
  - 分支 / 目录名：星体观测、深空档案、近邻星域、远域星区、天体巡测、生态记录、生物档案、野外观测、物种研究、任务编队、航行记录、载具观测、运输平台
- 对象通用字段【推断】：`id`、`type_id`、`name`、`version`/`revision`（对象会出新版本）、`tree_revision`、`timestamp_ms`、`position`（**可能为 null**；见过 `frame_id:"galaxy-…"`、`lat`、`coordinates`）、`attributes`（嵌套，深度和字段数可配）、`changes`、`tags`、`description`
- **位置有多种坐标系**【字面量确认，格式待抓包】：
  - `"position":{"frame_id":"galaxy-…"`：星系坐标
  - `"position":{"lat":…`：地表纬度一类
  - `"position":null`，另有 `"frame_kind"` 字段
  - 轨道类相对位置：`"orbit":{"primary":"reference_world","altitude_km":…}`、`"orbit":{"host_mass_solar":1,"semi_major_axis_au":…}`、`"parent_class":"planet","orbital_distance_km":…`、`heliocentric_distance_au`
  - 后端要做一个**位置适配器**，把每个对象归一成 `(frame, x, y)` 或"未定位"。
- 文本里有中文（华东观测站、深空观测目录、联合科考队……），搜索要支持中文。
- **刻意制造的"脏"不等于坏数据**：同一字段路径时而是数字、时而是字符串（`mixed_scalar_fields`）、`null_values`、深层嵌套、新字段、新类型。这些都是"合法但陌生"的数据，**绝不能拒收**，否则会把已确认的数据弄丢。

### 1.5 receipt 字段（验收方拿来核对的东西）【确认 tag 名，结构待看】
- 批次：`first_sequence`、`last_sequence`、`sent_bitmap`、`ack_bitmap`、`bitmap_encoding`、`attempted/acknowledged/unacknowledged_batches`、`attempted/acknowledged_objects`
- 重复：`duplicate_sent_bitmap`、`duplicate_ack_bitmap`
- 故障：`fault_sent_bitmap`、`fault_ack_bitmap`、`fault_rejected_bitmap`
- 树：`final_tree_revision`、`final_tree_nodes`、`tree_attempted/acknowledged_revision`、`tree_operations`、`tree_errors`、`tree_retries`
- 结构：`types_seen`、`type_frequencies`、`tracked_distinct_structures`、`tracked_field_paths`、`mixed_scalar_fields(+examples)`、`null_values`、`max_depth`、`sampled_objects`
- 性能：`http_2xx/429/4xx/5xx`、`timeouts`、`network_errors`、`p50/p95/p99`、`average/max_latency_ms`、payload bytes
- `fingerprint`：算法未知，做不到复算，README 里如实说明，其余各项全部对账。

---

## 2. 前 10 分钟：把推断变成事实（必须先做）

1. Docker Desktop 已装好（29.9.2，linux/arm64），当前虚拟机 **8 CPU / 7.75 GiB**，保持不变即可。
   - 题目里的 4C/4G/20G 是**我们系统所有容器合计的预算**，不是 Docker 虚拟机的大小。发生器另算 1C/256M，虚拟机自己也有开销，所以虚拟机必须比 4C/4G 大。预算靠 compose 里的容器限制来保证（见 3.2）。
   - 本机 shell 的 PATH 里找不到 `docker`，它实际在 `/Applications/Docker.app/Contents/Resources/bin/docker`。可以在 Docker Desktop → Settings → Advanced 里把 CLI 装到系统路径，或者在 `~/.zshrc` 里加一行 `export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"`。
2. 离线摸格式：
   ```sh
   cd /Users/apple/universe-observatory/task-package
   docker load -i flightgen-image.tar
   docker run --rm talkingdata/flightgen:exam-v4-f1 --help
   docker run --rm talkingdata/flightgen:exam-v4-f1 preview --help   # 再按 help 导出样例对象和分类树
   docker run --rm talkingdata/flightgen:exam-v4-f1 inspect --help
   ```
3. 写一个约 30 行的**抓包接收器**（宿主机 :8000，`/ingest`，把每个请求的方法、头、body 落盘），按《操作说明》启动发生器，低速跑一轮（例如 `batch_size 5, workers 1, objects_per_second 50, duration_seconds 20, scenario evolving, tree_every_batches 10`），然后取 `/status`、`/receipt`，并查看 `generator-runs/` 下的产物。
4. 在抓包器里做 4 个小实验，每个只需几秒：
   - 每 N 个请求回一次 500：观察重试间隔，以及**重试时幂等键 / Batch-Id 是否保持不变**
   - 对探针回 400：观察**发生器会不会重试 4xx**
   - 回 429 + `Retry-After: 1`：观察它是否遵守
   - 故意晚回 2–5 秒：确认默认 `timeout_ms`
5. 产出 `docs/protocol.md`，写明各种消息的样例、头部含义、探针长什么样、receipt 结构和 bitmap 编码。这份文档会成为 README 里"面对未知格式的探索过程"一节，是很好的加分材料。

---

## 3. 系统架构（推荐方案）

```
发生器 ──POST /ingest──▶ observatory（单容器 Go 服务，cpus=4, mem=4g, 命名卷）
                           │ 1 读完 body（限制大小，超限 413）→ 准入：队列满就 429 + Retry-After
                           │ 2 内容校验（fastjson）：语法坏 → 400；缺身份字段等 → 422 → 进隔离区
                           │ 3 幂等检查 (test_id, idempotency key)：已提交 → 直接 200（记一次"吸收重复"）
                           │ 4 投递给单写线程 → group commit：一个事务（一次 fsync）提交 N 个请求
                           │      批次记录 + 对象版本化 upsert + 树操作 + 计数增量，原子提交
                           │ 5 提交成功后才回 2xx
                           ├─ SQLite（WAL, synchronous=FULL）：建议每个 test_id 一个库文件
                           ├─ 内存聚合：每秒时间桶、类型计数、字段结构统计、位置采样、事件流
                           ├─ 查询 API（REST）+ SSE（1Hz 推送快照）
                           └─ 静态前端（React 构建产物，由同一进程托管）
```

**为什么选这套**：单进程、单容器，资源预算好控制，部署也最简单。SQLite 加 group commit 能做到"2xx 即已落盘"，吞吐靠合并 fsync 而不是牺牲持久性。Go 编译快、并发模型直观，Codex 写得稳，你自己也讲得清。如果单写线程成了瓶颈，有两条升级路线，写进 README 的"取舍"里：先把原始消息追加到自己的 WAL 再 ACK、索引异步追；或者按对象 id 分片写多个库。

### 3.1 正确性铁律（Codex 必须严格遵守）
1. **先持久化，后 ACK**：只在事务提交（fsync）成功之后回 2xx；出错回 503。
2. **整条消息原子**：一个请求要么全部入库、要么全部拒收。只要有一个对象非法，整条 4xx，因为契约写的是"保存整条消息"。
3. **幂等**：键是 `(test_id, X-Idempotency-Key 或 Batch-Id)`，以抓包结果为准。重复请求直接 200 且不重复应用；同一个键并发到达时，后到的等前一个的结果。
4. **按内容校验，不靠 `X-Flightgen-Kind` 作弊**：校验规则从正常样本里归纳，必备字段以抓包为准。这个头只用来事后给我们的检测器打分（混淆矩阵）。
5. **版本化 upsert**：每个对象只保留最高版本，旧版本或重复投递永远覆盖不了新版本（乱序也安全），版本历史另存。
6. **分类树按 revision 幂等应用**，并记录每次 add/move/rename 操作，供可视化回放。
7. **一切按 `test_id` 隔离**：同一个 seed 会生成相同的 id，混在一起数字就对不上 receipt。
8. 校验必须容忍 schema 漂移：新字段、新类型、字段类型变化、null 都是合法的。

### 3.2 性能 / 可靠性要点
- **容量估算**：操作说明里调速的示例就是 20000 个/秒，`0` 表示尽力发送，所以要按每秒 2 万条以上来设计。如果每条约 1 KB（以实测为准），不压缩时约 20 MB/s，20 GiB 磁盘大约 17 分钟就写满了。对策：原始 JSON 用 zstd 压缩（通常 4–8 倍）；只给最新版本建索引，历史版本压缩存储；设磁盘水位；系统页显示"按当前速率，磁盘还能撑多久"。
- 服务监听 **:8000，路径 `/ingest`**，跟操作说明里的示例完全一致，验收方可以原样复制命令。
- compose 里给每个服务写资源限制，**合计**不超过 `cpus: 4`、`mem_limit: 4g`。同时设 `memswap_limit` 等于 `mem_limit`，不让 swap 绕过限制；`GOMEMLIMIT≈3GiB`。
- **默认 compose 不写 `cpuset`**：验收机有几个 CPU 不确定，写死 `cpuset: "1-4"` 的话，CPU 少于 5 个的机器上容器会直接起不来。绑核（发生器钉在 CPU 0、我们用 1–4）只放在自己压测用的 `docker-compose.bench.yml` 里。
- **20 GiB 持久化空间**：数据放在命名卷里，应用自己统计占用并设水位（例如超过 18 GiB 就拒收并告警）。Docker Desktop 的虚拟磁盘要比 20 GiB 大，因为镜像和构建缓存也要占空间。
- **自证合规**：README 里贴出以下内容：
  - `docker stats --no-stream` 的输出：LIMIT 列显示 4GiB，CPU 不超过 400%
  - `docker inspect -f '{{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}' <容器>` 的输出
  - 系统页上显示的磁盘占用
- 数据放**命名卷**，不要 bind mount 到 macOS 目录，后者慢而且 fsync 语义不可靠。
- 磁盘水位：数据库超过约 18 GiB 时拒收并在 UI 告警；顺便估算"每 GB 能装多少对象"。
- 先测**发生器自身上限**：用一个立即回 200 的空接收器跑，得到天花板，再报告"我们达到上限的 X%"。
- 网络路径：官方路径是 `host.docker.internal → 宿主机 → 容器端口`，要绕 Docker Desktop 的用户态网络。另测一组"发生器接入同一个 docker network、直连容器名"的数据，两组都写进 README。
- 启动恢复：内存聚合从数据库重建；优雅退出时先排空队列。

---

## 4. 可视化内容（核心）

全站围绕 4 个问题组织，每个页面回答其中一个，每张图只回答一个子问题，**标题直接写结论**（例如"过去 60 秒平均入库 18,240 个/秒"）。顶部一行全局筛选器：**当前轮次 test_id、时间窗、家族**。

| 问题 | 页面 | 内容 | 图表形式 |
|---|---|---|---|
| 正在发生什么 | **① 总览 / 指挥中心**（首页） | 世界对象总数（主数字）、类型数、分类树 revision、当前入库速率、已确认批次、吸收重复、拒收坏数据 | KPI 指标卡 + 迷你趋势线 |
| | | 入库速率随时间变化（突发、爬坡、429 时刻） | 折线图（单序列）；**延迟另起一张图，共用时间轴，禁止双 Y 轴** |
| | | 世界构成随时间变化（4 个家族 + 未知） | 堆叠面积图 |
| | | 实时事件流：新类型出现 / 分类树节点新增·改名·迁移 / 对象升到 vN / 字段类型漂移 / 拒收坏数据 / 吸收重复 | 带图标的时间线列表，按秒聚合，防止刷屏 |
| | | **"世界日志"自动叙事**：本轮发现 X 类、Y 个对象，树演化到 rN（新增 a、改名 b、迁移 c），拒收 d 个坏探针，吸收 e 次重复，已确认批次 100% 对账一致 | 由聚合数据生成的一段文字，最直接地"阐释这个世界发生了什么" |
| 有什么、在哪里 | **② 宇宙大地图**（和 ① 合并成主屏，见 4.1） | 对象在空间里的分布，以及实时到达的信号 | 底层是累积密度，上层是实时抽样闪光，放大后显示单个对象的大类图标；详见 4.1 |
| 怎么组织的 | **③ 分类体系** | 类型树和每个节点的对象数（向上汇总），新增、改名、迁移的节点打徽标 | 可折叠树 / 冰柱图，按数量用单色阶上色 |
| | | 分类树按 revision 的演化 | 每个 revision 的操作数柱图（单序列，操作类型的细分放进 tooltip 和表格）；进阶做法：revision 滑块回放 |
| | | 类型数量长尾（最多 8192 类） | Top 30 横向柱图 + "其余 N 类" + 可搜索表格 |
| 它是什么 | **④ 对象探索** | 多维检索：家族、类型子树、中文全文、字段=值、有无位置、版本 > 1 | 筛选栏 + 虚拟滚动表格 |
| | | 对象详情 | 按家族定制的档案卡（星体：光谱型/温度/质量；生物：栖息地/食性；航行器：飞行状态/任务天数；外星：通讯方式/感官/形态）+ 原始 JSON 树 + **版本时间线，相邻版本做字段级 diff** |
| 陌生的东西怎么理解 | **⑤ 结构与陌生事物** | 每个类型推断出的字段路径：出现率、取值类型分布、null 率、最大深度、首次出现时间 | 字段树表格；取值类型用文字标签（如 `number 92% · string 8%`），混合类型字段标 ⚠ |
| | | 世界在"变陌生"：累计类型数、累计字段路径数随时间的增长 | 两张小折线图，不叠加 |
| 数据可信吗 | **⑥ 数据可信 / 对账** | 入库漏斗：收到请求 → 拒收（invalid / incomplete）→ 吸收重复 → 入库批次 → 入库对象 | 横向漏斗柱 |
| | | **与 receipt 逐项对账**：已确认批次全部入库（缺失 = 0）、重复未被重复计数、坏探针全部拒收（fault_ack = 0）、树 revision 和节点数一致、各类型数量一致、字段路径和混合类型字段一致、抽样对象内容一致、"已入库但发生器未收到确认"（超时）的数量 | 检查清单，每项带状态图标、文字、期望值和实际值 |
| | | **批次序列图**：每个像素代表一个批次序号，状态有：已确认且已入库 / 已确认但缺失 / 已入库但未确认 / 未确认 / 坏探针已拒收 | 像素条（状态色 + 图标图例 + 计数表）。这是"数据可信"最有说服力的一张图 |
| | | 检测器准确率 | 混淆矩阵（我们的判定 × 发生器标签） |
| | | 隔离区 | 被拒消息列表：原因 + 原始片段 |
| 系统撑得住吗 | **⑦ 系统性能**（时间紧可以并进总览） | 发生器目标速率 vs 实际入库速率（同单位，可以画在同一个轴上）；提交延迟 p50/p95/p99；队列深度和 429 次数；CPU / 内存 / 磁盘对比 4C/4G/20G 预算 | 折线图、小多图、仪表条 |
| | | 历次测试一览：test_id、参数、峰值和持续速率、p99、对账结果 | 表格，直接截图放进 README |

**优先级**：P0 先做主屏（①+②）、⑥ 对账、③ 分类树、④ 对象检索；P1 做 ⑤ 结构、⑦ 性能；P2 做时间轴回看、revision 回放、3D。

### 4.1 主屏：宇宙大地图（合并 ① 总览和 ② 星图），按层级由粗到细

```
┌──────────────────────────────────────────────────────────────────────┐
│ 宇宙万物观测站  轮次 run-007 ▾  入库 18,240/秒  对象 1,234,567  ✓ 对账通过 │
├─────────────┬──────────────────────────────────────┬─────────────────┤
│ 大类图例/开关 │ 宇宙 › galaxy-3 › 星域 B7（面包屑）      │ 实时事件流        │
│ ● 星体  61%  │                                      │ 10:32 新类型出现  │
│ ▲ 航行器 12% │        宇宙大地图（WebGL）             │ 10:33 节点改名    │
│ ◆ 生物  15%  │   当前层级只画当前层级的东西              │ 10:33 信号丢失 ×30│
│ ★ 外星  4%   │   实时流入在当前层级上以"脉动"体现        │ …                │
│ ■ 其他  8%   │                                      │ 世界日志（文字总结）│
│ 信号丢失 2%  │                                      │                  │
├─────────────┴──────────────────────────────────────┴─────────────────┤
│ 时间轴：入库速率曲线（突发、429 标记）；进阶：拖动回看                       │
└──────────────────────────────────────────────────────────────────────┘
  其他页签：分类树 │ 对象检索 │ 数据对账 │ 系统性能
```

**核心原则：浏览器的负担和入库速率无关。** 不管每秒进来 200 条还是 2 万条，每一层同屏最多画几千个元素；后端只推当前层级的汇总数据和少量抽样。

**层级（语义缩放，像地图从国家放大到街道）**：

| 层级 | 看到什么 | 每个元素代表 | 实时流入怎么体现 |
|---|---|---|---|
| L0 宇宙 | 所有星系 | 一个星系一个发光节点：大小 = 对象数，环形色条 = 大类构成 | 节点按流入速率脉动 |
| L1 星系 | 星系内的星域网格 | 一个格子 = 一个星域：亮度 = 对象数，可按大类筛选；标出黑洞、恒星等地标 | 格子按流入闪烁 |
| L2 星域 | 单个对象 | 大类图标（视窗内最多约 5000 个，超出就再细分格子） | 新对象浮现、更新出涟漪（视窗内抽样，每秒最多几百个） |
| L3 天体系统 | 一颗星或一颗行星周围 | 环绕它的卫星、在轨的航行器、地表的生物（来自轨道和地表坐标） | 同上 |
| 档案 | 单个对象 | 档案卡 + 原始 JSON + 版本历史 | 新版本到达时提示 |

- 后端为每个星系维护多级网格计数（例如第 0–6 级，最细一级 64×64），按大类计数。每入库一个对象，就在各级格子里加一；对象移动时，旧格子减一、新格子加一。这部分在内存里完成，代价很低；重启时从数据库重建。
- 星系、星域的划分以 preview 看到的数据为准。`frame_id` 看起来天然就是星系；程序里还有 `positionGridOffset`，位置可能本来就是"格子 + 偏移"，这对分层很有利。

**`position = null` 的语义**（采纳 Codex 的判断，同时加两条保护）：
- 解读为**观测受阻 / 信号丢失**，不是对象消失。地图上把它留在**最后一次已知位置**，换成暗淡的空心图标，标注"最后观测于 vN（时间）"。
- 从有位置变成 null，记一条"信号丢失"事件；从 null 恢复，记一条"重新捕获"事件。按星域汇总后，如果某片区域集中丢失信号，就在地图上标成"观测受阻区"。
- 保护一：**存储层原样保存 null**。"最后已知位置"是另外派生出来的展示字段，不能写回数据，因为对账时最新版本的内容必须和发生器一致。
- 保护二：用真实数据验证这个解读。如果同一个对象在版本之间时有时无，支持"观测受阻"；如果某些对象从来没有位置，那就是"未定位"（比如文档、服务这类非空间对象），左侧单独计数。null 也可能只是发生器随机注入的空值（receipt 里有 `null_values` 统计），所以 UI 上称"信号丢失"，README 里说明这是我们的解读。

**不同事件，不同效果**：
- 新对象：浮现亮点
- 对象升到新版本：原位置泛起涟漪
- 新类型首次出现：放大高亮，并在事件流置顶
- 信号丢失：图标变暗、变空心
- 重新捕获：图标恢复，并闪一下
- 坏数据和重复投递：不上地图，只进事件流和计数

**图标按大类，不按类型**：星体 ●、航行器 ▲、生物 ◆、外星智慧 ★、其他 ■；具体类型放在悬停和详情里。图标形状加大类颜色双重区分。

---

## 5. 前端方案（为什么这样最清晰）

- **技术栈**：React + Vite + TypeScript；**ECharts 5** 一个库覆盖折线、面积、柱、树、矩形树图、热力图和大数据量散点（`large` 模式），主题统一、胶水代码少；表格用 TanStack Table + 虚拟滚动；JSON 用一个现成的树形查看组件。**主屏大地图用 deck.gl**：WebGL 渲染，平移缩放流畅，几十万个点也不卡；用 `OrthographicView`，单个对象用 `IconLayer` / `ScatterplotLayer`，密度用 `ScreenGridLayer` / `HeatmapLayer`。其余图表都用 ECharts。
- **不推荐 Grafana / Streamlit**：Grafana 是额外的后台服务，会占 4G 预算，也表达不了分类树、对象档案、对账这些领域视图；Streamlit 在服务端渲染，会吃后台 CPU，高频刷新时卡。
- **流畅的关键在数据通路**：前端只读**预聚合**接口（计数器、每秒时间桶、采样点、结构统计），从不扫原始数据；SSE 每秒推一次快照；列表用游标分页；星图服务端采样或分桶。
- **视觉编码规则**（配色已用校验脚本验证过，暗色主题）：
  - 图表卡底色 `#1a1a19`，页面底色 `#0d0d0d`，主文字 `#f0efec`，次文字 `#c3c2b7`，弱化色 `#898781`，网格线 `#2c2c2a`
  - **家族色固定**，全站一致，按这个顺序：星体 `#3987e5`、航行器 `#d95926`、生物 `#199e70`、外星智慧 `#c98500`；**未知 / 未归类 / 其他"万物"用灰色 `#898781`**（"未知"不是一种身份，而是还没归类；"其他"是兜底类）
    - 用于堆叠、柱、折线：校验通过（相邻色最差 CVD ΔE 8.4）。4 个序列时必须有图例和直接标注
    - 用于散点星图：4 色同屏**不通过**（黄↔橙 ΔE 4.8），所以星图用"高亮一个家族、其余置灰"或者分面
  - 状态色只用于可信和健康类信息，并且一律配图标和文字：good `#0ca30c`、warning `#fab219`、serious `#ec835a`、critical `#d03b3b`
  - 数量大小用蓝色单色阶（树节点、热力图），不用彩虹色
  - 不用双 Y 轴；单个数字用指标卡；超过 7 类的东西用表格；每张图都有表格视图；有 tooltip，但 tooltip 不能是读数的唯一途径
  - 操作类型（add/move/rename）、取值类型（string/number/…）这些**第二套分类不要再分配新颜色**，用图标、文字标签或单色图表达，避免和家族色撞车

---

## 6. API 契约（让前后端可以并行开发，前端先用 mock）

```
POST /ingest                                    数据面（2xx = 已落盘）
GET  /api/runs                                  [{test_id, started_at, last_seen_at, objects, batches}]
GET  /api/overview?run=                         totals / rate / series(每秒桶) / families(每秒桶) / system
GET  /api/stream?run=                           SSE：每秒一次 overview 增量 + 事件
GET  /api/events?run=&after=                    [{ts, type, summary, ref}]  type ∈ new_type|tree_op|version|schema_drift|rejected|duplicate
GET  /api/tree?run=&revision=                   [{id, parent_id, name, path, depth, count, subtree_count, last_change:{op, rev}}]
GET  /api/tree/history?run=                     [{revision, adds, moves, renames, ts}]
GET  /api/types?run=&sort=count&limit=          [{type_id, name, path, family, count, field_count, mixed_fields, first_seen}]
GET  /api/types/{id}/schema?run=                [{path, present_ratio, types:{string,number,bool,null,object,array}, examples}]
GET  /api/objects?run=&family=&type=&subtree=1&q=&cursor=&limit=   {items:[...], next_cursor}
GET  /api/objects/{id}?run=                     {latest, versions:[{version, ts, diff}]}
GET  /api/map/frames?run=                         L0：[{frame, bounds, objects, family_mix, inflow}] + unplaced / lost 数量
GET  /api/map/cells?run=&frame=&z=&bbox=&family=  L1：[[gx,gy,n,lost,inflow]]（指定级别、视窗内的格子）
GET  /api/map/objects?run=&frame=&bbox=&limit=    L2：[[x,y,family,type_id,id,state]]  state ∈ observed|lost
GET  /api/map/system/{object_id}?run=             L3：绕它的卫星、在轨航行器、地表对象
GET  /api/map/pings?run=&frame=&bbox=&since=      实时抽样：[[x,y,family,event]]  event ∈ new|update|new_type|lost|reacquired
GET  /api/quality?run=                          {funnel, quarantine:[...], detector:{tp,fp,fn,tn}}
POST /api/reconcile?run=                        后端从 GENERATOR_URL/receipt 拉取，或者接收上传的 receipt JSON
                                                → {checks:[{name,status,expected,actual}], bitmap:{first_seq,last_seq,states}}
GET  /api/system                                cpu / rss / disk / db_bytes / wal_bytes / queue_depth / commit_p50/p99 / budget
```
对账的兜底方案：容器里可能访问不到宿主机 127.0.0.1:9001，所以另外提供 `scripts/reconcile.sh`：在宿主机用 curl 取 receipt，再 POST 给 `/api/reconcile`。

---

## 7. 测试矩阵（结果要写进 README）

| # | 场景 | 参数要点 | 要记录的数据 |
|---|---|---|---|
| 1 | 正确性 | evolving，故障默认开启，官方示例参数（200/s bursty，120 秒） | 对账全部通过的截图 |
| 2 | 吞吐上限 | steady，`rate 0`，batch_size 100/500，workers 8/16 | 持续入库速率、p99、CPU/内存；和空接收器天花板对比 |
| 3 | 突发 | bursty，运行中 `/rate` 调到 20000 | 有无错误、429 次数、延迟变化 |
| 4 | 崩溃恢复 | 运行中 `docker kill` 服务，然后重启 | **已确认批次缺失 = 0** |
| 5 | 长稳 | 10–15 分钟 | 内存是否平稳、磁盘增长、每 GB 对象数、20G 能容纳的世界规模 |
| 6 | stress | scenario=stress | 大对象和深嵌套下的表现 |

用 `scripts/run-test.sh` 一键执行：启动 → 轮询状态 → 取 receipt → 对账 → 落盘 `results/<test_id>.json`。这样 README 里的数字都可以复现。

---

## 8. 交付节奏：MVP 优先、随时可交付

**原则**：任何时刻，GitHub 上最新的那个 commit 都是一份能交的作品。每完成一档就 push 一次，不把 push 留到最后。

### 8.0 开考前准备（不占考试时间）
- Docker Desktop 已装好，虚拟机 8 CPU / 7.75 GiB，保持不变，**不要调成 4C/4G**（原因见第 2 节）。确认你自己的终端里能直接运行 `docker`；提前 `docker load` 发生器镜像。
- 提前拉好基础镜像，以 Dockerfile 实际用到的为准，例如 `docker pull golang:1.25-bookworm node:22-bookworm-slim debian:bookworm-slim`。
- **修好 GitHub 推送**：本机 `gh` 的 token 已失效（`gh auth status` 报 invalid），用 git 走代理访问 github.com 时也出现过 SSL 错误。开考前在你自己的终端里执行 `gh auth login`，建好空仓库，并真的 push 一次测试 commit。
- 把本文件交给 Codex，确认它能读到 `task-package/`。

### 8.1 分级交付
| 档位 | 最晚 push | 内容 | 意义 |
|---|---|---|---|
| T0 保底 | 第 30 分钟 | 仓库骨架、`docs/protocol.md`（实测的协议和数据格式）、README 初稿（题目理解、架构设计、计划）、compose 骨架 | 就算代码没跑通，也能证明"看懂了问题、有方案" |
| **T1 MVP** | **第 60 分钟（最晚 75）** | 端到端跑通的最小完整版，范围见 8.2 | **从这一档开始，不会交白卷** |
| T2 增强 | 第 90 分钟 | 性能（group commit、429 背压）、压测和崩溃恢复的真实数据、地图 L0/L1 | 拿性能和可靠性的分 |
| T3 完整 | 第 110 分钟 | 地图 L2/L3、结构页、打磨 | 拿体验和完整性的分 |
| 冻结 | 第 105 分钟起 | 只改 README、补测试结果、修 bug，不再加功能 | 防止最后关头改坏 |
| 提交 | 第 115 分钟 | 最终 push，记录 SHA | |

### 8.2 MVP 的范围：四个问题都要覆盖，每个做最简单的版本
- **后端**：`POST /ingest`。
  - 读完 body，做 JSON 校验，坏数据回 4xx。
  - 按幂等键去重，按版本号 upsert，按 test_id 隔离，分类树操作也要入库。
  - **事务提交成功后才回 2xx**。MVP 阶段可以先每个请求单独一个事务，group commit 留到 T2 再做。
- **对账**：用 `scripts/reconcile.sh` 或 `/api/reconcile`，至少核对五项：
  - 已确认的批次是否全部入库
  - 坏探针是否全部拒收
  - 重复投递有没有被多算
  - 分类树的 revision 和节点数
  - 各类型的数量
- **前端只做一个页面，把四个问题讲清楚**：
  - 有什么：KPI 指标卡、大类构成、类型列表或简单的树
  - 正在发生什么：速率曲线、事件流
  - 可信吗：对账结果面板
  - 撑得住吗：几个系统指标
  - 另外再加一个对象检索表和详情页。**地图不进 MVP。**
- **可复现**：从干净的 clone 执行 `docker compose up --build` 能跑起来，资源限制已经写好。
- **README v1 要披露的信息**：
  - 怎么运行、架构、协议探索的结论
  - 一次真实测试的结果（贴对账输出）
  - **已完成 / 未完成清单**
  - 关键假设和解读（例如"null = 信号丢失"）
  - 已知限制、AI 使用说明

### 8.3 时间表
| 时间 | 后端（Codex） | 前端（Claude） | push |
|---|---|---|---|
| 00–10 | 第 2 节的侦察，产出 `docs/protocol.md` | — | |
| 10–30 | 仓库、compose、Dockerfile、Go 骨架；README 初稿 | Vite 骨架、主题、MVP 单页布局（先用 mock 数据） | **T0** |
| 30–60 | ingest MVP、对账脚本、一次低速正确性测试 | MVP 单页接上真实 API | **T1 MVP** |
| 60–90 | group commit、429、压测、崩溃测试，记录数据 | 地图 L0/L1、分类树页、对账页 | **T2** |
| 90–105 | 修 bug、磁盘水位 | 地图 L2/L3、结构页 | **T3** |
| 105–115 | 冻结：README 写入测试结果表、取舍和限制；从干净 clone 验证一遍 | 同左 | |
| 115–120 | 最终 push，记录 SHA | | **提交** |

**止损规则**：
- 到第 45 分钟 MVP 还没跑通：砍掉前端，只保留后端、对账脚本和一个静态统计页，先把 T1 push 上去。
- 到第 75 分钟 T2 还没成型：放弃地图，把时间花在测试数据和 README 上。
- 不管进度如何，第 105 分钟一到就冻结。

## 9. 风险与对策
- **Docker 没装**：开考前装好，并配好资源。
- **GitHub 推不上去**：本机 gh token 已失效，git 走代理时出现过 SSL 报错。开考前修好并试推一次；考试中每到一档都 push，避免最后一次也推不上去。
- **构建时联网不稳**（Go 模块、npm 走代理）：`go mod vendor`，提交前端构建产物（或者配置 GOPROXY=https://goproxy.cn 和 npm 镜像），让 `docker compose up --build` 尽量只需要拉基础镜像。
- **探针语义不确定**：前 10 分钟抓包确认；第一次对账时用 `fault_rejected_bitmap` 验证。
- **fsync 延迟**（Docker Desktop 的虚拟磁盘）：尽早测出来，靠 group commit 摊薄。
- **20G 磁盘**：设水位；每轮一个库文件，便于清理旧轮次；必要时压缩原始 JSON。
- **前端范围膨胀**：严格按 P0 → P1 → P2 推进。

## 10. README 里可以预先写好的"已知限制"
单机单写线程；`fingerprint` 算法未公开，无法复算（其余各项全部对账）；结构统计如果做了采样就是近似值；星图坐标的语义取决于发生器的 position 字段；`host.docker.internal` 这条路径的网络开销不归我们的系统控制。
