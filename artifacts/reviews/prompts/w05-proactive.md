# 开发窗口 05：Proactive 修复

## 必须先读取的审查报告
1. `artifacts/reviews/r07-proactive.md` — 完整发现清单（4🔴 8🟡 6🟢）
2. `artifacts/reviews/r15-master.md` — 总报告
3. `artifacts/reviews/r13-session-fixes.md` — 了解本轮已修复（D6/D7/T8）

## 目标文件
- `astrmai/proactive/rhythm.py` — 节奏评估（🔴崩溃风险）
- `astrmai/proactive/diary_service.py` — 日记服务
- `astrmai/proactive/dream_scheduler.py` — 梦境调度
- `astrmai/proactive/decay_service.py` — 衰减服务
- `astrmai/proactive/heartflow/manager.py` — 心流管理
- `astrmai/proactive/heartflow/feedback_bridge.py` — 反馈桥

## 依赖
窗口 03（state）+ 窗口 04（memory）

---

## 🔴 严重（4 项）

### P5-1：rhythm.py getattr(None) 崩溃风险
- **文件**：`astrmai/proactive/rhythm.py:72-76`
- **当前代码**（近似）：
  ```python
  reply = getattr(config, "reply", None)
  base_freq = getattr(reply, "base_frequency", 0.7)  # reply 可能是 None!
  ```
- **问题**：`config` 缺少 `reply` 属性时，`reply = None`，随后 `getattr(None, ...)` 抛出 `AttributeError`。任何通过 `dispatcher._safety_check`/`heartflow` 等路径调用且 config 缺 `reply` 段的场景都会崩溃。
- **最小修复**：
  ```python
  reply = getattr(config, "reply", None)
  base_freq = getattr(reply, "base_frequency", 0.7) if reply is not None else 0.7
  ```

### P5-2：详见 r07-proactive.md
### P5-3：详见 r07-proactive.md
### P5-4：详见 r07-proactive.md

---

## 🟡 中等（8 项）

详见 `r07-proactive.md`，重点：
- `DiaryService.run_once` 的 `prompt_registry` None 时静默跳过（D18 已部分修复）
- `DreamScheduler.run_dream_cycle` 的 `session_id` 传递（T2 已修复）
- `DecayService.run_once` 不持久化（T8 已适配）
- `HeartflowManager` 会话生命周期管理
- `ProactiveTask` 调度与 `wakeup_service` 的并发控制

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/test_proactive_scheduler_refactor.py tests/test_heartflow_refactor.py tests/regression/proactive/ -q
```

## 成功标准
- 🔴 P5-1：getattr(None) 崩溃风险消除
- 🔴 4 项全部修复
- Proactive 相关测试全部通过
