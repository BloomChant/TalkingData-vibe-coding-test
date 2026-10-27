# 已观测协议与处理策略

依据：附件《发生器操作说明》、运行中的 `talkingdata/flightgen:exam-v4-f1` 和一次 30 秒 `evolving` 真实采样。这里区分实测事实、应用策略和未确认项，不把二进制内部名词当作完整线协议。

## 消息形状

分类消息：

```json
{"kind":"types","test_id":"run-a","revision":1,"changes":[{"op":"create","node":{"type_id":1,"parent_id":0,"name":"万物"}}]}
```

对象批次：

```json
{
  "kind":"objects","test_id":"run-a","batch_id":0,"tree_revision":1,
  "objects":[{
    "id":"run-a-0","sequence":0,"type_id":17,"name":"恒星 #0",
    "timestamp_ms":1792627199784,
    "position":{"frame_id":"galaxy-1","frame_kind":"galactic","unit":"ly","x":8010,"y":-7150,"z":-200},
    "attributes":{"body_kind":"star","spectral_class":"A","mass_solar":2.36,"surface_temperature_k":8115}
  }]
}
```

对象示例摘取了真实字段；省略了一部分属性，`type_id=17` 必须在分类树中先创建，不能将此摘录作为完整可直接发送的独立测试。

- 实测对象公共字段：`id, sequence, type_id, name, timestamp_ms, position, attributes`。
- 分类节点字段：`type_id, parent_id, name`。没有线协议 `family` 字段。
- 分类操作已观测到 `create / rename / move`。未观测到删除操作，应用对未知操作返回 422。
- `tree_revision` 在批次信封上，表示所关联分类版本；不是对象版本号。
- `sequence` 是生成序号。额外探针可能使用不连续序号，不能仅根据序号缺口推定丢数。
- 原始 `timestamp_ms` 是生成数据时间；UI 接收速率使用服务端入库时间桶，不将两者混淆。

## 位置与动态属性

已观测到 `galactic / ly`、`stellar_system / km`、`local_surface / m` 和 `position:null`。

坐标比较至少要求相同 `frame_id` 与单位。没有坐标变换和对象归属的明确契约时，不拼接成一个有物理尺度的全宇宙图。不从 null 推断失联；文档和任务类记录可一直没有位置。

`attributes` 必须为 JSON 对象，其内部字段不固定，数字、字符串、布尔、数组、嵌套对象和 null 都可以出现。字段类型变化属于合法动态结构，不被误判成故障。

## 可靠接收与异常

出题方契约：任意 2xx 表示完整消息已经可靠保存；非 2xx 或超时表示未收到成功确认，可能重试。

应用策略：

| 状态 | 含义 |
|---|---|
| 202 | 完整业务事务与原始请求已提交 |
| 200 | 同轮次、同消息键、同内容此前已提交 |
| 400 | JSON 无法解析、非有限数或无法编码的 Unicode |
| 409 | 同一幂等键或对象 ID 对应不同内容 |
| 413 | 请求超过 8 MiB |
| 422 | 公共结构或分类关联非法；整批隔离 |
| 429 | 单写锁拥塞，带 `Retry-After: 1` |
| 503 | 存储暂不可用、依赖分类版本未到达等可重试情况 |
| 507 | 接近应用磁盘准入水位 |

合法内容中的未知字段原样保留。结构非法的原文进入压缩隔离表，不进入正常对象表；同一异常重试累加 attempts。分类和对象批次中途失败均回滚整条事务。

消息体用于识别幂等，不信任故障标记头来判断是否合法。请求头中的 `X-Flightgen-*`、`Idempotency-Key` 等仅保留用于诊断。

## 回执不是完整内容标准答案

本次运行回执包含：`format_version, encoder_version, bitmap_encoding, config, stats, tree, sent_bitmap, ack_bitmap, injections`。

`tree` 包含尝试和确认到的 revision。`injections` 包含重复与故障的发送/确认/拒收位图。实测位图编码为 `lsb0`：每个字节的低位对应较小的批次编号。

离线 `inspect` 另外提供类型频率、字段路径、混合类型、结构 fingerprint、最终树节点数量等。不能把离线“从序列开头取 N 个”的检查结果直接等同于任意网络运行中实际成功接收的数据集合。

当前对账基于运行回执的可验证字段。没有核实的故障 source-batch 与探针批次映射，以及逐对象内容标准答案对比保留为 `unknown`。

## 已知探索结果

30 秒侦察接收 183 条请求，其中 30 条分类消息、153 条对象批次消息；共 3,060 条对象记录，包含 4 次重复投递和 2 次异常探针。按应用策略重放后，正常主流对象为 2,940，两个异常批次各 20 条均未进入对象表，分类 revision 为 30。

一次低速样本不能证明所有场景都覆盖。故障重试、突发、高并发、`stress` 大对象，以及长期规模仍需独立运行测试。
