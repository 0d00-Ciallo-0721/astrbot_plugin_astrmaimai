# 窗口5-Prompt06-GroupReplyWaitManager并发保护修复审查报告

## 审查范围
- `astrmai/state/group_wait/group_reply_wait_manager.py`
- `tests/unit/state/test_group_reply_wait_manager_concurrency_migrated.py`
- `tests/original_ported/test_group_reply_wait_manager_ported.py`
- `tests/original_ported/test_group_wait_thread_signature_ported.py`
- 关联调用点：
  - `astrmai/app/plugin_facade.py`
  - `astrmai/conversation/execution/followup_manager.py`

## 审查结论
- 本窗口相关改动已复核通过。
- 未发现新的功能回归、并发一致性缺口或本窗口残留问题。
- 本轮可以将 `GroupReplyWaitManager` 并发保护修复视为完成闭环。

## 已确认修复事实

### 1. `_states` 常规访问已收口
- `register_from_reply_event()`、`handle_incoming_message()`、`cancel_wait()`、`get_wait_info()` 对 `_states` 和 `_timeout_tasks` 的关键访问已统一走同步可用锁。
- 常规读写路径不再存在此前的裸读裸写问题。

### 2. timeout 误删保护有效
- timeout 协程不再无条件 `pop(chat_id)`。
- 现在会校验“当前 state 是否仍是自己创建的那一份”，旧 timeout 不会误删后续重新注册的新 wait。

### 3. 并发双注册覆盖问题已关闭
- 已针对“旧 timeout task 的 cancel 阻塞时，同 chat 并发双注册”做定向复核。
- 当前实现中，“弹出旧 task + 写入新 state”已处于同一临界区内。
- 复现实验确认最终保留的是最新注册状态，不再出现旧注册覆盖新注册的问题。

### 4. 回归测试稳定性已改善
- 新增并发回归测试不再依赖过窄时间窗。
- 已增加确定性线程交织测试覆盖“并发双注册”场景。
- 原 timeout 回归已保留，并放宽时间窗口，避免脆弱的 `remaining_seconds > 0` 断言。

## 审查验证

### 已执行测试
- `pytest tests/unit/state/test_group_reply_wait_manager_concurrency_migrated.py -q`
- `pytest tests/original_ported/test_group_reply_wait_manager_ported.py -q`
- `pytest tests/original_ported/test_group_wait_thread_signature_ported.py -q`
- `pytest tests/unit/state -q`
- `pytest tests/original_ported -q -k group_wait`
- `pytest tests/test_chat_loop_kernel_refactor.py -q -k group_wait`
- `pytest tests/test_system2_runner_refactor.py -q`

### 附加事实核验
- 已复跑“阻塞旧 task.cancel() 的并发双注册”定向复现实验。
- 复核结果：最终 `get_wait_info()` 保留最新注册目标，符合预期。

## 备注
- 运行中仍可见仓库既有 warning：
  - `jieba/pkg_resources` 弃用 warning
  - `after_nonebot_init was never awaited` 运行时 warning
- 本轮未引入新的 warning 类型，也未发现与本窗口改动直接相关的失败。

## 最终结论
- 本窗口无历史遗留问题。
- `Prompt06 / GroupReplyWaitManager 并发保护修复` 已通过深度代码审查。
