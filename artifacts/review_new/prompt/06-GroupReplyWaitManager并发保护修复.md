# Prompt 06：`GroupReplyWaitManager` 并发保护修复

## 任务目标
修复 `GroupReplyWaitManager` 中 `_states` 在正常读写路径上未完整受锁保护的问题。

本轮只做：
- `_states` 的一致性保护
- timeout / register / handle / cancel 等路径的并发边界

## 必读报告
- `artifacts/review_new/05-模块-M5-状态主动性与工作模式.md`
- `artifacts/review_new/13-汇总-AstrMai最终审查报告.md`

## 必读代码
- `astrmai/state/group_wait/group_reply_wait_manager.py`
- 涉及 `_states` 的所有入口方法
- 相关测试

## 必须完成的修复
1. 找出当前哪些 `_states` 路径在锁外运行。
2. 明确锁粒度，避免 timeout 协程和普通消息路径互相踩状态。
3. 补最小回归，覆盖“并发消息 + timeout + cancel” 至少一种交织场景。

## 实施要求
- 优先收口共享状态访问，不要顺手改业务规则。
- 如果需要新增 helper，保持作用域局部。

## 验证要求
至少执行：
- 相关 state / group_wait 测试
- 新增或更新最小并发回归

## 完成标准
- `_states` 常规路径不再裸写裸读
- 并发状态竞争面收窄
- 更新：
  - `artifacts/review_new/05-模块-M5-状态主动性与工作模式.md`

