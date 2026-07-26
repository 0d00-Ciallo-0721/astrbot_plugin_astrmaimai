# OPT-01 群聊在途回复丢弃止血（线程签名时序）

状态：代码完成（待线上 trace 复采验收） ｜ 优先级：P0 ｜ 依赖：无 ｜ 覆盖发现：ID-01(P0)、TL-05(P2)、ID-07(P3/LIKELY) ｜ 用户正在受害的第一大 bug。

## 目标

- 群聊里 @bot/点名提问后，bot 已生成的回复不再被**无关新消息**静默击杀；只有同线程的更新活动才参与 stale 判定。
- 消灭误导性 `tool model failed, trying next` WARN（实为 stale 被误分类成模型失败，还会换模型重跑烧双倍 dialog 成本）。
- 基线 → 目标：最活跃群 16h stale_drop 7 条（> executed 6 条）→ 接近 0；`newer_activity_unknown_thread` 从 stale 原因中基本消失。

## 基线证据

- `gate.py:527-536` `_record_event_activity` 在事件**到达时**就把 `event.get_extra("astrmai_thread_signature", None)` 传给 `mark_activity`——但该 extra 要到 prompt 构建阶段才由 `legacy_compat.py:76/171` 写入（全库 grep 证实仅此两处生产写入点），活动记录的线程签名**恒为空**。
- `chat_runtime_coordinator.py:447-477` 的隔离三分支（`different_known_thread`、same-thread salvage 窗、`allow_parallel_threads`）全部要求两侧签名非空 → 整套线程隔离是死代码；任何用户任何新消息（delta>4s）都以 `unknown_thread` 击杀在途回复。
- trace 实证：stale 原因清一色 `newer_activity_unknown_thread`（4 例 age 11.5-29.8s，max_age 450s），`same_thread`/`other_thread_ignored` 从未出现过；日志 15:23:22 实锤一张 bot 自己都忽略的被动图片杀死了正在回答直接提问的回复。
- TL-05：`reply_freshness.py:50` `is_stale_reply_reason` 匹配前缀 `superseded_by_newer_activity:`（带冒号），而真实 reason 是 `..._same_thread:`/`..._unknown_thread:`（下划线接续）——永不匹配；executor `except Exception → continue` 把 stale 当模型失败换模型重跑（日志 3 次实锤 executor:1081）。

## 实施步骤

1. **先固化现状测试**（OPT-13/TG-01 的一部分可提前）：写"ingress 时刻 mark_activity 携带的线程标识非空"的失败测试，以及"B 的无关消息不杀 A 的在途回复"的端到端用例（当前应红）。
2. `gate.py::_record_event_activity`：改传 `resolve_group_thread(event, chat_id).thread_id`（turn 绑定时已可用，见 `message_entry` 的 `astrmai_turn_thread_id`），不再依赖后置的 `astrmai_thread_signature`。
3. `chat_runtime_coordinator.evaluate_reply_freshness`：比较基准同步改为 turn thread_id；确认 `executor._evaluate_execution_freshness` 补传 `allow_parallel_threads`。
4. `reply_freshness.is_stale_reply_reason`：前缀改为 `superseded_by_newer_activity`（去冒号），一行修复同时覆盖三种 reason 格式。
5. executor 预检路径补记 `stale_category`（3/7 stale 样本该字段全空），并停止把 freshness 中止记为 `tool model failed`（`executor.py:1081` 分支先做 `is_stale_reply_reason` 判定，stale 直接终止本轮不换模型）。
6. ID-07（LIKELY，低频）：`group_reply_wait_manager.py:329` 的 unique-target 兜底条件放宽为 `not incoming_thread_signature`（turn_thread_id 不算 explicit），或 register 时同时登记 `sender:<target>` 别名键——先写复现单测（reply: 键等待 + 纯文本跟进应 RESUME），红了再修。

## 验收标准

- 新增回归测试全绿：双线程隔离用例、`is_stale_reply_reason('superseded_by_newer_activity_unknown_thread:a:5.0s') is True`、reason 生产/消费契约测试。
- 全量 pytest 绿；部署后 24h trace 复采：stale_drop 中 `unknown_thread` 占比 ≈0，`same_thread`/`other_thread_ignored` 开始出现（证明隔离活了）；日志不再出现 stale reason 的 `tool model failed` WARN。
- 群内实测：A @bot 提问，5s 后 B 发无关消息，A 的回复仍然发出。

## 风险与回退

- **中风险**：放开跨线程并行后同群并发回复增多，依赖 send_claim/GroupActorConsistency 兜底——灰度观察 24h，若并发回复异常可先把 `allow_parallel_threads` 关回（保留签名修复，只收紧并行开关）。
- TL-05 是纯字符串前缀放宽，语义方向一致，低风险。
- 回退：三处改动各自独立提交，任一异常单独 revert 即回到现状（现状=全杀，不会更糟）。

## 完成记录

**2026-07-26 代码侧完成**（线上 trace 复采验收待部署后执行）：

- 改动文件（4 个，均为最小边界）：
  - `astrmai/conversation/execution/reply_freshness.py`：① `is_stale_reply_reason` 前缀 `superseded_by_newer_activity:` → 去冒号宽前缀（TL-05）；② `_check_reply_freshness` 线程标识改为 turn identity → `astrmai_turn_thread_id` → 旧 focus 签名三级回退（ID-01 检查侧）。
  - `astrmai/conversation/attention/gate.py::_record_event_activity`：mark_activity 改传 turn thread id（缺失时回退 `resolve_group_thread`），两个调用点（fast-wakeup L821 / 主路径 L1205）同时受益（ID-01 标记侧）。
  - `astrmai/conversation/execution/executor.py`：`_evaluate_execution_freshness` 同步 turn 标识空间 + 补 `allow_parallel_threads=not is_private`；`_check_pre_model_freshness` 预检终止时补记 stale 观测字段（此前 3/7 stale 样本字段全空）。
  - `astrmai/state/group_wait/group_reply_wait_manager.py`：unique-target 兜底条件放宽为"无 focus 签名且 turn 线程非 `reply:*`"（ID-07；`sender:*` 缺省回退不算显式线程，`reply:*` 引用与 focus 签名仍阻止劫持——该边界由既有反劫持测试逼出，比 OPT 文档原方案更精确）。
- 新增回归测试：`tests/regression/conversation/test_group_reply_drop_hotfix.py` 12 条（首跑 6 红 6 绿，红项精确命中三个缺陷；修复后 12/12 绿）。
- 更新过时断言 1 处：`test_attention_private_chat_ported.py::test_fast_wakeup_path_marks_runtime_activity` 的 `assertIsNone(thread_signature)` 恰好把 ID-01 缺陷固化成了契约，改为断言 `sender:user-1`。
- 全量回归：**1679+ passed**；仅余 3 个失败为 `test_group_signin_service_refactor.py` 的**历史遗留时间窗 flaky**（`group_signin_service.py:39` 按 `tm_hour==SIGN_HOUR` 判定、测试未注入时钟，仅在签到时段能过；已在改动前代码树上复现，与本 OPT 无关，归 OPT-13 处理）。
- 待部署验收：24h trace 复采看 stale_drop 中 `unknown_thread` 占比 ≈0、出现 `same_thread`/`other_thread_ignored`、无 stale reason 的 `tool model failed` WARN；并发回复灰度观察 `send_claim`/actor guard 兜底表现。
