# 去重与交叉验证记录

## 验证方法

- **A级交叉验证**：至少两份领域报告指向同一生产根因，并由主线程重新读取关键源码段确认。
- **B级闭环验证**：单份报告具备入口、触发、调用链、实际/期望行为和源码锚点；主线程确认当前生产文件存在且路径可达。
- **条件型确认**：需要特定部署或故障条件，但源码状态转换成立。进入修复前先补最小复现，不作为误判删除。

12 份报告引用了 118 个唯一生产文件。路径扫描发现 5 个旧目录别名，已在批次文件中校正：

| 报告中的旧路径 | 当前真实路径 |
|---|---|
| `astrmai/config.py` | `config.py` |
| `astrmai/memory/memory_engine.py` | `astrmai/memory/services/memory_engine.py` |
| `astrmai/memory/dream/dream_scheduler.py` | `astrmai/proactive/dream_scheduler.py` |
| `astrmai/conversation/context/context_engine.py` | `astrmai/conversation/planning/context_engine.py` |
| `astrmai/app/lifecycle_helpers.py` | `astrmai/shared/helpers/plugin_helpers.py` 与 `astrmai/app/lifecycle.py` |

## 合并的重复计数

| 最终修复 ID | 合并的原始发现 | 去重判断 |
|---|---|---|
| `R01-01` | `FFA-02-001`, `FF-01`, `FFA-09-002` | 同一私聊状态机根因：无 waiter 时仍缓存并返回 `PRIVATE_WAIT` |
| `R01-02` | `FFA-02-002`, `FFA-09-001` | 同一 `PerceptionBuilder` raw group ID 根因，同时造成跨 origin 混池和 state 双记录 |
| `R01-08` | `FFA-02-008`, `FFA-09-003` | 同一 threaded wait 注册/查找身份不一致 |
| `R02-06` | `FFA-ENTRY-004`, `FF-09` | 同一 error fallback 未终止 AstrBot event |
| `R03-04` | `FFA-03-004`, `AM-LP-10-13` | 同一 Planner `reply_text is None` 终止契约缺失，覆盖已发送 fallback 和未发送两种结果 |
| `R07-01` | `AM-MEM-06-03`, `AM-LP-10-05` | 同一每日 canonical memory decay 缺少必填参数 |
| `R08-02` | Assignment 08 profile flush, `FFA-09-008` | 同一画像即时持久化签名错误 |
| `R08-03` | Assignment 08 wakeup persistence, `AM-LP-10-06` | 同一 wakeup cooldown 持久化签名错误 |
| `R08-04` | `FFA-ENTRY-005`, Assignment 08 EventBus | 同一 EventBus 单例队列跨重载保留；原 partial 由独立报告和源码确认升级为 confirmed |

上表合并减少 10 个原始计数：私聊簇减少 2，其余 8 个二项簇各减少 1。最终为 `106 - 10 = 96` 个独立修复单元。

## 高相关但不合并

- `R01-08` 与 `R01-09`：前者是 wait manager 的 thread key 不一致，后者是 ChatLoopKernel 仅保存 chat-wide wait；修复位置不同。
- `R02-02` 与 `R02-07`：前者在 ingress 把外部结果当 self message 丢弃，后者在 synthetic event 丢失 group/private scope；属于连续链上的两个缺陷。
- `R03-08`、`R03-09`、`R04-01`、`R04-02`：都与回复状态有关，但分别是 failed claim、stale history、follow-up key 和 partial delivery。
- `R02-05`、`R03-03`、`R06-06`、`R08-01`、`R08-05`、`R08-06`、`R09-06`：共享“热配置传播不完整”模式，但拥有不同 live object 和派生字段，不合并成一个不可验证的大修复。
- `R06-03` 与 `R06-09`：都属升级迁移，但一个是 FTS projection 未回填，一个是读取错误 legacy DB。
- `R09-02` 与 `R09-03`：一个是双消费者批次所有权，一个是单消费者部分提交的幂等性。
- `R07-07`、`R07-08`、`R09-08`：都是损坏默认文本，但进入不同数据流并产生不同持久化影响。

## 严重度处理

- 合并项采用最高严重度。
- P3 画像 flush 合并到 P2，因为独立持久化报告证明每条消息确定性失败并存在崩溃窗口。
- P2 wakeup persistence 合并到 P1，因为成功主动回复后的 cooldown 在重启后确定丢失，可造成重复可见消息。
- Provider 能力推断、迁移大数据量、重载队列等保留为条件型 confirmed；修复前必须先构造触发条件。

## 数量核对

| 项目 | 数量 |
|---|---:|
| 原始报告发现 | 106 |
| 重复计数消除 | 10 |
| 误判/不可达排除 | 0 |
| 最终独立修复单元 | 96 |
| 修复轮次 | 11 |
