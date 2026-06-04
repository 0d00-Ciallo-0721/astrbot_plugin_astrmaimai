# 开发窗口 03：State/Mood/Relationship 修复

## 必须先读取的审查报告
1. `artifacts/reviews/r06-state.md` — 完整发现清单（3🔴 8🟡 5🟢）
2. `artifacts/reviews/r15-master.md` — 总报告
3. `artifacts/reviews/r13-session-fixes.md` — 了解本轮已修复内容

## 目标文件
- `astrmai/state/chat_state_service.py` — StateEngine 核心
- `astrmai/state/mood/mood_decay.py` — 衰减逻辑
- `astrmai/state/mood/mood_manager.py` — 情绪分析
- `astrmai/state/relationship/relationship_engine.py` — 关系引擎
- `astrmai/proactive/decay_service.py` — DecayService

## 依赖
窗口 02（infrastructure persistence）— ChatState 持久化必须先修复。

---

## 🔴 严重（3 项）

### P3-1：mood_decay config.energy 无空安全访问
- **文件**：`astrmai/state/mood/mood_decay.py:11,19`
- **当前代码**（行 14）：
  ```python
  recovery_min = getattr(config.energy, "recovery_silence_min", 60)
  ```
- **问题**：`getattr` 只保护了**二级**属性（`recovery_silence_min`），但 `config.energy` 本身是直接属性访问——若 config 没有 `energy` 属性则 `AttributeError`。
- **同样问题**（行 19）：`config.mood.decay_interval` 没有一级保护。
- **最小修复**：
  ```python
  _energy = getattr(config, "energy", None)
  recovery_min = getattr(_energy, "recovery_silence_min", 60) if _energy is not None else 60
  ```
  同理处理 `config.mood`。

### P3-2：详见 r06-state.md
### P3-3：详见 r06-state.md

---

## 🟡 中等（8 项）

详见 `r06-state.md`，重点：
- `StateEngine.update_mood` 的 CAS 三阶段（snapshot→LLM→write）中 `_get_state_inner` 返回值与 `get_state` 不一致时的处理
- `apply_natural_decay` 对 `state` 对象字段（energy/last_reply_time/last_passive_decay_time）的隐式依赖
- `RelationshipEngine.align_social_score` 调用链中 `UserProfileService` 的同步问题
- `DecayService.run_once` 不持久化（仅 in-place decay）的职责分离是否充分文档化

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/test_state_services_refactor.py tests/regression/state/ -q
```

## 成功标准
- 🔴 P3-1：config.energy/config.mood 一级属性有空安全保护
- 🔴 P3-2/P3-3 修复
- State 相关测试全部通过（T6 已修复，T12-T13 在窗口 02 修）
