# Design Document

> 本文档对应 Spec `astrmai-medium-polish`，描述 13 项 🟡/🟢 级完善需求的设计方案。

## 1. Overview

| Wave | 需求数 | 改动类型 | 行数估计 |
|:----:|:--:|------|:--:|
| ① 配置完善 | 3 | JSON schema + 校验逻辑 | +18 |
| ② 韧性缺口 | 3 | 重启逻辑 + 门控 | +25 |
| ③ 人设打磨 | 4 | fallback + 选项 + 清理 | +30 |
| ④ 可观测性 | 3 | 计数器 + 日志 | +15 |
| **合计** | **13** | | **~+88** |

### 设计边界

- 不修改业务逻辑
- 不新增 pip 依赖
- R10 清理死代码不删除文件
- R8 新功能默认关闭

---

## 2. Wave 1 — 配置完善（R1–R3）

### 2.1 R1: `_conf_schema.json` 数字范围

**涉及文件**: `_conf_schema.json`

**设计决策**: 对所有数值字段增加 `"minimum"` / `"maximum"`。

```json
// 概率字段示例：
"base_frequency": {
    "type": "float", "default": 0.7,
    "minimum": 0, "maximum": 1
}
// 正整数字段示例：
"max_steps": {
    "type": "int", "default": 5,
    "minimum": 1
}
```

**影响**: 1 文件，+40（约 40 个数值字段增加范围标记）

---

### 2.2 R2: `emotion_mapping` 格式校验

**涉及文件**: `config.py`

**设计决策**: 在 `AstrMaiConfig.__init__` 互斥检测之后增加 emotion_mapping 校验。

```python
# config.py AstrMaiConfig.__init__ 增加：
for entry in self.reply.emotion_mapping:
    if ":" not in entry:
        logger.warning(f"[AstrMai] emotion_mapping entry missing colon: {entry!r}")
```

**影响**: 1 文件，+4

---

### 2.3 R3: 模型池名称校验

**涉及文件**: `config.py`

**设计决策**: 在 `AstrMaiConfig.__init__` 中增加模型名格式启发式检测。

```python
for pool_name, pool in [("agent", self.provider.agent_models), ("task", self.provider.task_models), ("vision", self.provider.vision_models)]:
    for model in pool:
        if "/" not in model:
            logger.warning(f"[AstrMai] model in {pool_name}_models missing provider prefix (no '/'): {model!r}")
```

**影响**: 1 文件，+6

---

## 3. Wave 2 — 韧性缺口（R4–R6）

### 3.1 R4: EventBus Worker 自动重启

**涉及文件**: `astrmai/infrastructure/runtime/event_bus.py`

**当前状态**: 3 个 worker 启动后无健康检查，全部崩溃后事件永久丢失。

**设计决策**: 新增 `_worker_health_check()` 协程，每 30s 检查并补足 worker。

```python
async def _worker_health_check(self):
    while self._workers_started:
        await asyncio.sleep(30)
        active = sum(1 for t in self._background_tasks if not t.done())
        needed = 3 - active
        for _ in range(max(0, needed)):
            task = asyncio.create_task(self._worker_loop())
            self._background_tasks.add(task)
        if needed > 0:
            logger.warning(f"[EventBus] restarted {needed} worker(s), now {active + needed} total")
```

在 `_start_workers()` 中启动健康检查。

**影响**: 1 文件，+15

---

### 3.2 R5: ProactiveTask Loop 自动重启

**涉及文件**: `astrmai/proactive/proactive_task.py`

**当前状态**: `start()` 创建 task 但无 done callback，外部取消后 loop 永久停止。

**设计决策**: 增加 `_on_loop_done` callback + 重启逻辑。

```python
def _on_loop_done(self, task):
    self._background_tasks.discard(task)
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        if self._is_running:  # 意外取消 → 重启
            logger.error("[ProactiveTask] loop unexpectedly cancelled, restarting in 5s")
            asyncio.get_event_loop().call_later(5, lambda: asyncio.create_task(self._loop()))
        return
    if exc and self._is_running:
        logger.exception("[ProactiveTask] loop crashed, restarting in 5s")
        asyncio.get_event_loop().call_later(5, lambda: asyncio.create_task(self._loop()))
```

**影响**: 1 文件，+10

---

### 3.3 R6: 记忆置信度门控

**涉及文件**: `astrmai/memory/services/memory_write_service.py`, `config.py`, `_conf_schema.json`

**设计决策**: 在 `write()` 方法中增加 confidence 检查。

```python
# memory_write_service.py write() 开头增加：
min_conf = getattr(self, "_min_confidence", 0.3)
if request.confidence < min_conf:
    logger.debug(f"[MemoryWrite] skipped low-confidence memory ({request.confidence:.2f} < {min_conf})")
    return None
```

配置：
- `config.py` `MemoryConfig`: `min_memory_confidence: float = Field(default=0.3, ge=0.0, le=1.0)`
- `_conf_schema.json` `memory.items`: 新增配置项

**影响**: 3 文件，+8

---

## 4. Wave 3 — 人设打磨（R7–R10）

### 4.1 R7: Persona 摘要 fallback 改进

**涉及文件**: `astrmai/memory/persona/persona_summarizer.py`

**当前状态**: 3 次 LLM 重试失败后 `original_prompt[:150]` 裸截断。

**设计决策**: 增加关键句提取逻辑。

```python
# _summarize_core_identity_with_retry() except 块修改：
lines = [l.strip() for l in original_prompt.split("\n") if l.strip()]
key_lines = [l for l in lines if any(kw in l for kw in ("你是", "角色", "身份", "设定", "性格", "名字"))]
if key_lines:
    fallback = "；".join(key_lines[:3])
else:
    fallback = "；".join(lines[:3])
return f"[系统降级提取-LLM重试失败] {fallback[:150]}"
```

**影响**: 1 文件，+5/-3

---

### 4.2 R8: self_lore 自动注入选项

**涉及文件**: `astrmai/conversation/planning/context_engine.py`, `_conf_schema.json`, `config.py`

**设计决策**: 可选配置 + 在 `_load_persona_payload()` 中条件注入。

```python
# context_engine.py _load_persona_payload() 末尾：
if getattr(self.config.persona, "include_self_lore_in_prompt", False):
    lore_text = await self.self_lore_service.recall_persona_lore(
        query="角色设定", persona_id=target_persona_id
    )
    if lore_text:
        payload["self_lore"] = lore_text
```

配置：
- `config.py` `PersonaConfig`: `include_self_lore_in_prompt: bool = Field(default=False)`
- `_conf_schema.json` `persona.items`: 新增

**影响**: 3 文件，+12

---

### 4.3 R9: Persona 缓存过期检测

**涉及文件**: `astrmai/memory/persona/persona_summarizer.py`

**设计决策**: 在缓存 JSON 中存储 `source_hash`，加载时比对。

```python
# get_summary() 中：
source_hash = hashlib.sha256(original_prompt.encode()).hexdigest()[:12]
cached = self._load_cache(cache_key)
if cached and cached.get("source_hash") != source_hash:
    logger.info(f"[Persona] source changed for {cache_key}, rebuilding")
    cached = None
# 存储时：
cache_entry["source_hash"] = source_hash
```

**影响**: 1 文件，+6

---

### 4.4 R10: FrequencyController 死代码清理

**涉及文件**: `astrmai/app/bootstrap.py`, `astrmai/conversation/attention/gate.py`, `astrmai/app/runtime_context.py`

**设计决策**: 方案 A（清理）。从注入链路移除，保留文件 + 标注。

```python
# gate.py — 删除 self.frequency_controller 赋值
# bootstrap.py — 删除 FrequencyController 实例化
# runtime_context.py — 删除 InteractionServices.frequency_controller 字段
# frequency_controller.py — 文件顶部增加注释
"""
# DEPRECATED — unused in current pipeline.
# Frequency control is now handled by EnergyManager + AttentionGate throttle.
"""
```

**影响**: 4 文件，-8（删除注入代码），+0（保留文件 + 注释）

---

## 5. Wave 4 — 可观测性（R11–R13）

### 5.1 R11: Lane rotation 资源泄漏指标 + R12: 对话膨胀监控

**涉及文件**: `astrmai/infrastructure/runtime/lane_manager.py`, `astrmai/app/runtime_context.py`

**设计决策**: 两个计数器，通过 `build_diagnostics()` 暴露。

```python
# lane_manager.py __init__ 新增：
self._rotation_count = 0
self._active_lane_count = 0

# ensure_lane() rotation 触发时：
self._rotation_count += 1

# ensure_lane() 创建/切换后：
self._active_lane_count = len(self._lane_locks)

# runtime_context.py build_diagnostics() 新增：
"lane": {
    "rotation_count": self.lane_manager._rotation_count if self.lane_manager else 0,
    "active_lane_count": self.lane_manager._active_lane_count if self.lane_manager else 0,
}
```

**影响**: 2 文件，+12

---

### 5.2 R13: 启动阶段日志完善

**涉及文件**: `astrmai/app/lifecycle.py`

**设计决策**: 每个 `set_boot_phase()` 后增加 `logger.info`。

```python
# on_program_start() 中：
await self.initialize_memory()
logger.info("[AstrMai] boot phase: lifecycle.memory complete")

await self.load_command_metadata()
logger.info("[AstrMai] boot phase: lifecycle.commands complete")

await self.start_proactive_services()
logger.info("[AstrMai] boot phase: lifecycle.proactive complete")
# ... 其他阶段同理
```

**影响**: 1 文件，+6

---

## 6. Risk Assessment

| # | 风险 | 等级 | 缓解 |
|---|------|:--:|------|
| RSK1 | R4 worker 重启检查每 30s → 最坏情况 30s 事件丢失 | 🟢 | 可接受 |
| RSK2 | R5 `call_later` 在非主线程可能失败 | 🟡 | 改用 `asyncio.get_event_loop().create_task` |
| RSK3 | R6 confidence 门控可能丢弃 LLM 产生的正确记忆 | 🟢 | 默认 0.3 很低，仅过滤极低置信度 |
| RSK4 | R9 首次部署时无 `source_hash` → 触发全量重建 | 🟢 | 仅首次，后续正常 |
| RSK5 | R10 清理后如有未知引用 → ImportError | 🟡 | 先搜索全项目引用确认 |

## 7. Verification Matrix

| # | 需求 | 验证方式 | 通过标准 |
|---|------|---------|---------|
| V1 | R1 | JSON schema 含 `minimum`/`maximum` | 40+ 字段有范围标记 |
| V2 | R2 | 含无冒号条目 → warning | 日志输出 |
| V3 | R3 | 含无斜杠模型名 → warning | 日志输出 |
| V4 | R4 | 模拟 worker 崩溃 → 30s 内恢复 | 活跃 worker=3 |
| V5 | R5 | 模拟 loop 异常 → 5s 后重启 | loop 恢复 |
| V6 | R6 | `confidence=0.1` → skip + debug 日志 | 不写入 |
| V7 | R7 | LLM 全部失败 → 关键句提取 | fallback 含"你是"等 |
| V8 | R8 | `include_self_lore_in_prompt=True` → payload 含 self_lore | payload["self_lore"] 非空 |
| V9 | R9 | 修改人设文本 → 缓存重建 | source_hash 不匹配触发重建 |
| V10 | R10 | 全项目搜索 `FrequencyController` 引用 | 仅文件自身引用 |
| V11 | R11 | `/runtime/status` 含 `lane_rotation_count` | API 返回指标 |
| V12 | R12 | `/runtime/status` 含 `active_lane_count` | API 返回指标 |
| V13 | R13 | 启动日志含 `boot phase:` | 6 条阶段日志 |

## 8. Summary

| # | 文件 | 改动 | 行数 |
|---|------|------|:--:|
| 1 | `_conf_schema.json` | R1: 数值范围 + R6: confidence + R8: self_lore | +48 |
| 2 | `config.py` | R2: mapping 校验 + R3: 模型名校验 + R6: confidence + R8: self_lore | +12 |
| 3 | `event_bus.py` | R4: worker 健康检查 | +15 |
| 4 | `proactive_task.py` | R5: loop 重启 | +10 |
| 5 | `memory_write_service.py` | R6: confidence 门控 | +5 |
| 6 | `persona_summarizer.py` | R7: fallback 改进 + R9: staleness | +11/-3 |
| 7 | `context_engine.py` | R8: self_lore 注入 | +5 |
| 8 | `bootstrap.py` | R10: 删除 FrequencyController 注入 | -3 |
| 9 | `gate.py` | R10: 删除 fc 引用 | -1 |
| 10 | `runtime_context.py` | R10: 删除 fc 字段 + R11-R12: lane 指标 | +6/-4 |
| 11 | `lane_manager.py` | R11-R12: 计数器 | +8 |
| 12 | `lifecycle.py` | R13: 启动阶段日志 | +6 |
| 13 | `frequency_controller.py` | R10: 标注 deprecated | +3 |
| **Total** | **13 文件** | | **~+129 / -11** |

---

> **设计文档完成。** 可进入 Phase 3（任务文档）或直接执行。


