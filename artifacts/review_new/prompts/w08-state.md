# 开发窗口 08：State — CAS 并发安全 + Config 空安全

## 必须先读取的审查报告
1. `artifacts/review_new/r07-state.md` — 4🔴 7🟡 6🟢

## 审查范围
`astrmai/state/`（10 个源文件）

---

## 🔴 严重（4 项）— 全部 P0

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `chat_state_service.py:215-228` | **CAS 双衰减竞争**。Phase 1 对缓存 state 做 `apply_natural_decay` 突变，Phase 3 再衰减一次 → 能量恢复两次。**修复**：Phase 1 不做 decay，仅快照原始 mood；或深拷贝后 decay。 |
| 2 | `chat_state_service.py:310-314` | **`consume_energy` 锁外保存**。`mark_energy_consumed` 释放锁后 `save_chat_state` + `is_dirty=False`，另一协程可能覆盖。**修复**：save 内聚到锁内。 |
| 3 | `user_profile_service.py:460-465` | **`flush_message_counters` 提前清除脏标记**。`is_dirty=False` 在 `_save_profile` 之前，保存失败则变更永久丢失。**修复**：移到保存成功后。 |
| 4 | `energy/energy_manager.py:21,29` | **Config 空安全缺失**。`self.config.energy.cost_per_reply` 和 `min_reply_threshold` 直接访问，无 `getattr` 兜底。**修复**：统一使用 `getattr` 链式回退。 |

---

## 🟡 中等（重点 4 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 5 | `mood/mood_decay.py:13-16` | 能量恢复缺少幂等性守卫（与 #1 关联），增加时间戳守卫 |
| 6 | `relationship/relationship_engine.py:233-236` | 共振放大逐维度累加 → 事件级一次应用（`streak_bonus^4` vs `streak_bonus`） |
| 7 | `chat_state_service.py:278-283` | `analyze_mood` 接口签名不统一，统一为 `**kwargs` 可扩展签名 |
| 8 | `energy/energy_manager.py:30-45` | `should_drop_by_energy` 副作用隐藏，调用后未触发持久化 |

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/test_state_services_refactor.py tests/regression/state/ -q
```

## 成功标准
- 🔴 4 项全部修复
- 🟡 #5 #6 修复
- 17+ 相关测试通过
