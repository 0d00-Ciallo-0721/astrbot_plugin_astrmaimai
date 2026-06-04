# Agent 07

Agent ID:
`019e71c0-72b2-74a2-8d3f-5b3f25cca983`

状态：
已完成

发现：
1. [dispatcher.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/dispatcher.py:195) 和 [wakeup_service.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/wakeup_service.py:165) 之间还有一个同步完成路径的状态回写错误。`complete()` 先把 intent 标成 `sent/reply_sent=True`，但 `dispatch()` 在 `inject_external_event()` 返回后又用旧的 `decision` 覆盖 history（[dispatcher.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/dispatcher.py:293)）。结果是同轮内已经成功发出的主动消息，返回值和 dispatcher history 仍显示 `queued` / `reply_sent=False`。最小复现里，`WakeupService.run_for_chat()` 返回 `performed=False`，但能量已经扣减、`next_wakeup_timestamp` 也已经推进。
2. [dream_scheduler.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/dream_scheduler.py:149) 的全局冷却仍有竞态窗口。`run_once_for_session()` 在进 `semaphore` 前做 `describe_session_eligibility()`，但拿到锁后不复查；而 `_last_dream_time` 要到真正跑完后才更新（[dream_scheduler.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/dream_scheduler.py:81), [dream_scheduler.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/proactive/dream_scheduler.py:135)）。并发跑两个 session 时，两个都成功执行了 dream cycle，绕过了代码声明的 `throttle_scope="global"`。
3. [heartbeat.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/workmode/cron_guard/heartbeat.py:31) / [heartbeat.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/workmode/cron_guard/heartbeat.py:39) 以及 [heartbeat.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/workmode/cron_guard/heartbeat.py:84) / [heartbeat.py](/C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/workmode/cron_guard/heartbeat.py:90) 还有 `job_id` 归一化不一致的问题。`active_job_ids` 被统一成字符串，但 `snap.job_id` 比较时没转字符串；只要快照侧是 `int`/UUID 对象、运行中 job 是字符串，就会被误判成“丢失任务”并重复 revive。

补充：
- 这轮复检里，没有再发现“heartflow 只是状态堆积、不真正影响行为”的明确问题。
- 在本窗口检查范围内，也没看到“主动唤醒把 synthetic 文本直接写回主链 anchor”的新问题；主动事件本身仍是空 `message_str` 注入。

验证：
运行了 `python -m unittest tests.test_proactive_scheduler_refactor tests.test_cron_guard_refactor tests.test_heartflow_refactor tests.regression.proactive.test_dream_maintenance_migrated -q`，40 个测试通过；另外补做了 3 个最小复现脚本，分别确认了上面 3 个问题。
