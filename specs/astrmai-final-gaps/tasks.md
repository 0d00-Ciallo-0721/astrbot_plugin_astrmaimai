# Design + Tasks（4 项最终缺口）

## R1: LLM 延迟计时（H7）

**设计**: `gateway_call.py` L173 前 `t0 = time.perf_counter()` → L206 后 `latency = (time.perf_counter() - t0) * 1000` → 传入 `_log_usage(latency_ms=latency)`。

**任务**: 
- [ ] ① `gateway_call.py:_elastic_call_result()` L173 前加 `t0`，L206 后计算 `latency_ms`
- [ ] ② `gateway_result.py:_log_usage()` 签增加 `latency_ms: float = 0`，日志中输出

## R2: DB 运行时保护（H10）

**设计**: `_get_state_inner()` / `mark_energy_consumed()` / `atomic_update_mood()` 中 DB 调用包裹 try/except，异常时降级。

**任务**:
- [ ] ③ `chat_state_service.py:_get_state_inner()` L95 包裹 try/except → 返回 `_create_default_state(chat_id)` + logger.exception
- [ ] ④ `chat_state_service.py:mark_energy_consumed()` L150 包裹 try/except → logger.exception + continue
- [ ] ⑤ `chat_state_service.py:atomic_update_mood()` L138 包裹 try/except → logger.exception + return current_mood

## R3: self_lore 注入（R8）

**设计**: `context_engine.py` 已有 `self.summarizer.memory_engine` 属性 → 通过它调用 `recall_persona_lore()`。

**任务**:
- [ ] ⑥ `context_engine.py:_load_persona_payload()` 中：若 `config.persona.include_self_lore_in_prompt=True` → 调用 `self.summarizer.memory_engine.recall_persona_lore("角色设定", target_persona_id)` → 存入 `payload["self_lore"]`

## R4: Persona 缓存过期（R9）

**设计**: `get_summary()` L184 缓存命中时比对 `raw_hash`。

**任务**:
- [ ] ⑦ `persona_summarizer.py:get_summary()` L184-L211 增加：`if cached_data.get("raw_hash") != self._compute_hash(original_prompt): del self.cache[cache_key]; cached_data = None`
- [ ] ⑧ `persona_summarizer.py:get_summary()` L246 缓存存储时增加：`new_cache_data["raw_hash"] = self._compute_hash(original_prompt)`

## Summary

| # | 文件 | 改动 | 行数 |
|---|------|------|:--:|
| 1 | `gateway_call.py` | 延迟计时 | +3 |
| 2 | `gateway_result.py` | latency_ms 参数 | +2 |
| 3 | `chat_state_service.py` | 3 处 try/except | +12 |
| 4 | `context_engine.py` | self_lore 注入 | +5 |
| 5 | `persona_summarizer.py` | 缓存过期 | +4 |
| **Total** | **5 文件** | | **~+26** |
