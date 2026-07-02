# Design Document — AstrMai 审计后加固

> 本文档对应 Spec `astrmai-post-audit-hardening`，描述第六轮审计后残留缺陷的设计方案。
> 基于 `requirements.md` 中 18 条需求（R1–R18），按 5 个 Wave 展开模块设计。
>
> **不包含**：安全加固、框架配置、新功能开发、架构重构。
> **凡涉及时间源变更的改动，本阶段一律先方案后落地。**

---

## 1. Overview

### 1.1 整体策略

按「日志 → 配置 → 时间源 → 集合 → 测试」五阶段推进，每阶段独立可验证：

| 阶段 | 主要动作 | 改动文件 | 改动类型 |
|------|---------|---------|:--:|
| ① Wave 1 | `except Exception:` 补日志 | ~18 | 纯加 `logger.exception/warning` |
| ② Wave 2 | `config.py` 补字段 + `_conf_schema.json` 同步 | 2 | 模型字段新增 |
| ③ Wave 3 | `max(0, delta)` 钳制 + 注释标注 | ~10 | 加 guard + 注释 |
| ④ Wave 4 | dict/set 加 TTL/上限清理 | ~5 | 加清理方法 |
| ⑤ Wave 5 | 测试 mock 同步 + 新增测试 | ~5 | 测试代码 |

### 1.2 设计边界

| 禁止项 | 原因 |
|--------|------|
| **不修改 DB schema** | 无新增表/列/索引 |
| **不修改 API 契约** | 不改变函数签名、返回值类型、配置 JSON 结构（仅补字段） |
| **不替换 DB 边界处的 `time.time()` → `time.monotonic()`** | epoch 不同，数据库值使用 Unix epoch |
| **不修改 `proactive/rhythm.py`** | 依赖 `time.localtime()` 必须使用墙上时钟 |
| **不新建独立模块/文件** | 除测试文件外，仅修改现有文件 |

### 1.3 与已完成修复的接口

| 已完成项 | 预留接口 | 本 Spec 使用方式 |
|---------|---------|----------------|
| `safe_create_task()` in `shared/helpers/plugin_helpers.py` | 公共工具函数 | Wave 3/4 中新建任务可使用 |
| `_prune_stale_focus_pools()` in `gate.py` | 清理扩展点 | Wave 4 R12 在此扩展清理 `_proactive_injection_lock` |
| `_last_focus_pool_prune` 守卫 | 300s 间隔调度 | Wave 4 复用此守卫模式 |
| `monotonic()` imports in 23 files | 已导入 | Wave 3 不新增 monotonic 引用（用 max-guard 替代） |
| `event.stop_event()` in `message_entry.py` | 消息阻断 | Wave 1 日志变更不影响 |

---

## 2. Architecture

### 2.1 系统总体形态（变更前后对比）

```
变更前（问题状态）:
─────────────────────────────────────────────
config      ┌─ _conf_schema.json (4 字段孤岛)
            └─ config.py (模型缺 4 字段)
            
exception   ┌─ gate.py: 4× silent pass
handling    ├─ executor.py: 3× silent pass
            ├─ persona_summarizer: 8× silent pass
            ├─ chat_state_service: 5× silent pass
            └─ ... (~30 more)
            
timing      ┌─ 28 sites: time.time() → monotonic() ✅ (Phase 5 done)
            ├─ 18 sites: time.time() × DB boundary → NTP vuln
            └─ 7  sites: mixed → unlabeled

collections ┌─ 7 dicts grow without TTL/size limit
            └─ 3 dicts already fixed (Phase 4) ✅

变更后（目标状态）:
─────────────────────────────────────────────
config      ┌─ _conf_schema.json ↔ config.py (全对齐)
            
exception   ┌─ 全链路: except Exception → logger.exception/warning
            
timing      ┌─ 28 sites: monotonic() ✅
            ├─ 18 sites: max(0, delta) guard
            └─ 7  sites: ponytail comment labeled

collections ┌─ 全链路: TTL/上限清理
```

### 2.2 模块依赖图

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   main.py    │────→│ message_entry│────→│    gate.py   │
│  (Hook入口)   │     │  (消息准入)   │     │  (注意力门)   │
└──────┬───────┘     └──────────────┘     └──────┬───────┘
       │                                        │
       ▼                                        ▼
┌──────────────┐                       ┌──────────────┐
│ plugin_facade│──────────────────────→│   planner    │
│   (门面)      │                       │  (规划器)     │
└──────┬───────┘                       └──────┬───────┘
       │                                      │
       ▼                                      ▼
┌──────────────┐     ┌──────────────┐  ┌──────────────┐
│  lifecycle   │────→│ memory_engine│  │    judge     │
│  (生命周期)   │     │  (记忆引擎)   │  │   (判官)     │
└──────────────┘     └──────────────┘  └──────────────┘
       │
       ▼
┌──────────────┐     ┌──────────────┐
│persistence   │     │ chat_state   │
│  (持久化)     │     │  (状态管理)   │
└──────────────┘     └──────────────┘

Wave 1 触及: gate, executor, planner, memory_engine, chat_state, persona_summarizer, persistence + webui
Wave 2 触及: config.py, _conf_schema.json
Wave 3 触及: database_service, memory_retrieval, session_memory_summarizer, v2_store, judge, cognitive_loop, reply_freshness, relationship_engine, mood_decay, chat_state, user_profile, promotion_engine, hybrid_retriever, memory_scoring
Wave 4 触及: gate, chat_state_service, memory_engine, private_chat_manager
Wave 5 触及: tests/
```

### 2.3 关键不变量（本 Spec 阶段冻结）

| 不变量 | 来源 | 冻结理由 |
|--------|------|---------|
| `time.time()` 不替换为 `time.monotonic()` where DB boundary | R9–R11 | epoch 不同，数据库存 Unix epoch |
| `safe_create_task()` 签名不变 | `shared/helpers/plugin_helpers.py:23-37` | 已广泛使用，不破坏调用方 |
| `config.py` 模型结构不变（仅补字段） | `config.py` | 不改变字段名/类型/层级 |
| `_conf_schema.json` 的 `items` 包裹结构不变 | `_conf_schema.json` | AstrBot 框架约定 |
| `event.stop_event()` 调用点不变 | `message_entry.py` (Phase 2) | 已修，不回退 |
| `gate._prune_stale_focus_pools()` 签名不变 | `gate.py` (Phase 4) | Wave 4 在此上扩展 |

---

---

## 3. Wave 1 — P0 静默异常日志补全（R1–R4）

### 3.1 R1+R2: Gateway 层 + 批量日志补全

**涉及文件**: `gate.py`, `executor.py`, `context_compaction.py`, `vision_binding.py`, `gateway_lane.py`, `gateway_result.py`, `persona_summarizer.py`, `chat_state_service.py`, `private_chat_manager.py`, `mood_manager.py`, `event_utils.py`, `cron_agent.py`, `database_profile_relation.py`, 等

#### 3.1.1 当前状态

以 `gate.py:155` 为例：
```python
# gate.py:153-156 — 当前
try:
    return bool(self.sensors.is_wakeup_signal(event, self_id))
except Exception:
    return False       # ← 静默吞没，无日志
```

`persona_summarizer.py` 中 8 处类似：
```python
# persona_summarizer.py:457 — 当前
try:
    ratio = self._calculate_expressiveness_ratio(...)
except Exception:
    ratio = 0.5       # ← 静默降级，无日志
```

#### 3.1.2 设计决策

**统一补日志模式**：
```python
# 模式 A: 降级路径（返回默认值）
try:
    return bool(self.sensors.is_wakeup_signal(event, self_id))
except Exception:
    logger.warning(f"[AstrMai] is_wakeup_signal failed for {event.unified_msg_origin}, degrading to False", exc_info=True)
    return False

# 模式 B: 继续执行路径（pass）
try:
    self._do_something()
except Exception:
    pass
# → 改为:
try:
    self._do_something()
except Exception:
    logger.debug(f"[AstrMai] non-critical operation failed: {exc}", exc_info=True)
```

**原则**：
- 消息准入链路（gate.py）→ `logger.warning` + `exc_info=True`
- 人格/记忆降级（persona_summarizer, memory 管线）→ `logger.exception()`（自动含堆栈）
- 清理/资源释放（executor.py temp_file）→ `logger.debug`
- WebUI 路径 → `logger.exception()`

#### 3.1.3 影响范围

| 文件 | 处数 | Severity | 预计行数 |
|------|:--:|------|:------:|
| `gate.py` | 4 | warning | +4 |
| `executor.py` | 3 | debug/warning | +3 |
| `context_compaction.py` | 4 | debug | +4 |
| `vision_binding.py` | 2 | debug | +2 |
| `persona_summarizer.py` | 8 | exception | +8 |
| `chat_state_service.py` | 5 | warning | +5 |
| `private_chat_manager.py` | 2 | warning | +2 |
| 其余 ~10 文件 | ~20 | debug/warning | +20 |
| **合计** | **~48** | | **+48 行** |

#### 3.1.4 禁止改动

- **不**改变异常捕获类型（`except Exception` 保持不变）
- **不**改变降级返回值
- **不**重构现有 `logger.debug/error` 块

---

### 3.2 R3: Persona Summarizer 专项

**涉及文件**: `astrmai/memory/persona/persona_summarizer.py`

#### 3.2.1 当前状态

```python
# lines 457-683: 8 个切片计算方法，每个都有独立的 except Exception: 降级
# 示例 (line ~457):
try:
    expressiveness_ratio = self._calculate_ratio(chat_id, ...)
except Exception:
    expressiveness_ratio = 0.5  # 静默降级，无法知道哪个切片失败

# line ~494: response_style 切片 — 同样静默
# line ~526: emotional_tone 切片 — 同样静默
# ... 共 8 处
```

#### 3.2.2 设计决策

每处添加 `logger.exception()` 含切片标识：
```python
except Exception:
    logger.exception(f"[AstrMai] persona slice 'expressiveness_ratio' failed for {chat_id}")
    expressiveness_ratio = 0.5
```

切片标识命名（与代码中的方法名对应）：
1. `expressiveness_ratio`
2. `response_style`
3. `emotional_tone`
4. `vocabulary_richness`
5. `sentence_complexity`
6. `interaction_pattern`
7. `topic_preference`
8. `temporal_pattern`

#### 3.2.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| `persona_summarizer.py` | 8 × `+logger.exception(...)` | +8 |

#### 3.2.4 禁止改动

- **不**改变切片计算逻辑
- **不**改变降级默认值

---

## 4. Wave 2 — P1 配置模型/模式同步（R5–R8）

### 4.1 R5: `enable_token_estimator` 加入 ConversationConfig

**涉及文件**: `config.py`

#### 4.1.1 当前状态

```python
# _conf_schema.json:176-181 — 已定义
"enable_token_estimator": {
    "type": "bool", "default": false,
    "hint": "开启后，上下文压缩将基于 Token 估算值..."
}

# config.py:149 — ConversationConfig 模型 — 无此字段
class ConversationConfig(BaseModel):
    enable_context_compaction: bool = True
    compaction_trigger_ratio: float = 0.82
    # ← enable_token_estimator 缺失
```

```python
# shared/constants/defaults.py:100 — 消费方永远回退
estimator_enabled = getattr(
    getattr(getattr(runtime, "config", None), "conversation", None),
    "enable_token_estimator", False  # ← 永久 False，因为模型无此字段
)
```

#### 4.1.2 设计决策

在 `ConversationConfig` 中添加字段：
```python
class ConversationConfig(BaseModel):
    enable_context_compaction: bool = True
    compaction_trigger_ratio: float = 0.82
    enable_token_estimator: bool = False  # ← 新增
```

**兼容性**：`defaults.py:100` 的 `getattr` 回退路径保留不变 → 即使模型缺字段也安全。加入后 `getattr` 将命中模型属性，用户配置生效。

#### 4.1.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| `config.py` | +1 字段 | +1 |

#### 4.1.4 禁止改动

- **不**移除 `defaults.py` 中的 `getattr` 回退
- **不**修改 `_conf_schema.json`

---

### 4.2 R6+R7: 其余 3 个缺失配置字段

**涉及文件**: `config.py`, `_conf_schema.json`

#### 4.2.1 当前状态

```python
# config.py — 三个模型各缺字段:
class EvolutionConfig(BaseModel):       # line 95
    # ← review_runner_interval_sec 缺
    # ← review_runner_min_interval_sec 缺

class MemoryConfig(BaseModel):          # line 163
    # ← auto_recall_probability 缺
```

```python
# 消费方当前全部走 getattr 回退:
# bootstrap.py:446
getattr(getattr(runtime.config, "evolution", None), "review_runner_interval_sec", 60)
# expression_auto_check_task.py:38
getattr(self.config.evolution, "review_runner_min_interval_sec", 45)
# context_engine.py:517
getattr(getattr(self.config, "memory", None), "auto_recall_probability", 0.0)
```

#### 4.2.2 设计决策

```python
# config.py — 补字段:
class EvolutionConfig(BaseModel):
    # ... existing fields ...
    review_runner_interval_sec: int = 60       # ← 新增
    review_runner_min_interval_sec: int = 45   # ← 新增

class MemoryConfig(BaseModel):
    # ... existing fields ...
    auto_recall_probability: float = 0.0       # ← 新增
```

```json
// _conf_schema.json — evolution.items 下补:
"review_runner_interval_sec": {
    "type": "int", "default": 60, "hint": "表情审查运行间隔（秒），范围 30-600"
},
"review_runner_min_interval_sec": {
    "type": "int", "default": 45, "hint": "表情审查最小间隔（秒），防止同一聊天连续触发"
}

// _conf_schema.json — memory.items 下补:
"auto_recall_probability": {
    "type": "float", "default": 0.0, "hint": "自动记忆召回概率（0.0=禁用, 1.0=每次触发）"
}
```

#### 4.2.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| `config.py` | +3 字段 | +3 |
| `_conf_schema.json` | +3 字段定义 | +15 |

#### 4.2.4 禁止改动

- **不**修改消费方的 `getattr` 回退路径（保持兼容层）
- **不**改变字段的命名风格（snake_case 保持一致）

---

### 4.3 R8: 全局对齐验证

**涉及文件**: `config.py`, `_conf_schema.json`

#### 4.3.1 设计决策

**验证流程**（手动 + grep 辅助）：
```powershell
# Step 1: 提取 _conf_schema.json 所有字段名
# Step 2: 提取 config.py 所有 Pydantic 模型字段名
# Step 3: diff 输出差异清单
```

产出物：差异报告（Markdown 表格），无代码修改。

#### 4.3.2 验收产出

```markdown
## _conf_schema.json ↔ config.py 对齐报告
| 字段名 | schema 有? | model 有? | 状态 |
|--------|:--------:|:--------:|:----:|
| enable_token_estimator | ✅ | ✅ (after R5) | OK |
| review_runner_interval_sec | ✅ | ✅ (after R6) | OK |
| review_runner_min_interval_sec | ✅ | ✅ (after R6) | OK |
| auto_recall_probability | ✅ | ✅ (after R7) | OK |
| ... (其余字段遍历确认) | | | |
```

---

## 5. Wave 3 — P1 时间源 DB 边界修复（R9–R11）

### 5.1 R9: DB 查询截止时间保护

**涉及文件**: `database_service.py`, `memory_retrieval_service.py`, `session_memory_summarizer.py`, `v2_store.py`

#### 5.1.1 当前状态

```python
# database_service.py:146 — 当前
cutoff_timestamp = time.time() - float(max_age_seconds)
# → 若 NTP 回拨 10s，cutoff_timestamp 比刚写入的数据还早 10s → 数据丢失

# v2_store.py:1086 — 当前
cutoff = self._now() - older_than_seconds
await db.execute(delete(CanonicalJargon).where(CanonicalJargon.created_at < cutoff))
# → 同风险
```

#### 5.1.2 设计决策

**max-guard 钳制模式**：
```python
# database_service.py:146 — 修复后
now = time.time()
cutoff_timestamp = now - float(max_age_seconds)
if cutoff_timestamp > now:  # NTP 回拨检测
    logger.warning(f"[AstrMai] NTP backward jump detected: cutoff={cutoff_timestamp} > now={now}, clamping")
    cutoff_timestamp = 0.0  # 钳制为 epoch，查全量
```

```python
# memory_retrieval_service.py:353 — 修复后
delta = max(0.0, time.time() - item.created_at)
# delta 永不为负，保护下游 scoring 逻辑
```

```python
# v2_store.py:1086 — 修复后
now = self._now()
cutoff = now - older_than_seconds
if cutoff > now:
    logger.warning(f"[AstrMai-v2] clock skew: cutoff={cutoff} > now={now}")
    cutoff = 0.0
```

#### 5.1.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| `database_service.py:146` | +3 guard | +3 |
| `memory_retrieval_service.py:353` | `max(0, ...)` | ±1 |
| `session_memory_summarizer.py:43` | +3 guard | +3 |
| `v2_store.py:1086` | +3 guard | +3 |
| `v2_store.py:1135` | +3 guard | +3 |

#### 5.1.4 禁止改动

- **不**替换 `time.time()` → `time.monotonic()`
- **不**修改 DB 查询逻辑（WHERE 条件、JOIN 等）

---

### 5.2 R10: 聊天链路时间比较保护

**涉及文件**: `judge.py`, `cognitive_loop.py`, `reply_freshness.py`

#### 5.2.1 当前状态

```python
# judge.py:191 — 当前
now = time.time()
if now - timestamp > max_age_seconds:  # delta 可能为负（NTP 回拨）
    continue

# cognitive_loop.py:682 — 当前
idle_seconds = time.time() - last_reply_time  # 可能为负

# reply_freshness.py:55 — 当前
reply_age = time.time() - event_ts  # 可能为负
```

#### 5.2.2 设计决策

```python
# judge.py:191 — 修复后
now = time.time()
delta = now - timestamp
if delta < 0:
    logger.warning(f"[AstrMai-judge] clock skew: msg timestamp {timestamp} > now {now}")
    delta = 0  # 钳制，视为"刚刚发生"
if delta > max_age_seconds:
    continue

# cognitive_loop.py:682 — 修复后
idle_seconds = max(0.0, time.time() - last_reply_time)

# reply_freshness.py:55 — 修复后
reply_age = max(0.0, time.time() - event_ts)
```

#### 5.2.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| `judge.py:191` | +4 guard | +4 |
| `cognitive_loop.py:682` | `max(0, ...)` | ±1 |
| `reply_freshness.py:55` | `max(0, ...)` | ±1 |

---

### 5.3 R11: 状态存储时间源注释标注

**涉及文件**: `relationship_engine.py`, `mood_decay.py`, `chat_state_service.py`, `user_profile_service.py`, `promotion_engine.py`, `hybrid_retriever.py`, `memory_scoring.py`

#### 5.3.1 设计决策

纯注释变更 — 在每个 `time.time()` 与 DB/外部时间戳交互的站点添加标注：
```python
# ponytail: wall-clock, mixed with DB values — do NOT replace with monotonic
now = time.time()
```

#### 5.3.2 标注站点清单

| 文件 | 行号 | 上下文 |
|------|------|--------|
| `relationship_engine.py` | 86-88 | `first_seen`, `last_interaction`, `last_decay_time` |
| `mood_decay.py` | 8 | `now` vs `state.last_reply_time` |
| `chat_state_service.py` | 90, 116 | `now` for state TTL |
| `user_profile_service.py` | 109, 116 | `now` for profile touch |
| `promotion_engine.py` | 79 | `now_ts` mixed with DB timestamps |
| `hybrid_retriever.py` | 79 | time decay vs DB `create_time` |
| `memory_scoring.py` | 64 | temporal scoring vs DB timestamps |

#### 5.3.3 影响范围

| 文件数 | 改动 | 行数估计 |
|:--:|------|:------:|
| ~7 | 每处 +1 行注释 | +7 |

---

## 6. Wave 4 — P2 无限集合清理（R12–R15）

### 6.1 R12: `gate._proactive_injection_lock` 随 `focus_pools` 同步清理

**涉及文件**: `astrmai/conversation/attention/gate.py`

#### 6.1.1 当前状态

```python
# gate.py:94 — 当前: focus_pools 已有清理 (Phase 4), 但 _proactive_injection_lock 未清理
self.focus_pools: Dict[str, SessionContext] = {}
self._proactive_injection_lock: dict[str, asyncio.Lock] = {}  # line 85

# gate.py:_prune_stale_focus_pools — Phase 4 新增
def _prune_stale_focus_pools(self, max_age: float = 86400.0):
    now = monotonic()
    stale = [cid for cid, s in self.focus_pools.items() if now - float(s.last_active_time) > max_age]
    for cid in stale:
        self.focus_pools.pop(cid, None)  # ← 清理 focus_pools
        # ← 但 _proactive_injection_lock[cid] 未清理！
```

#### 6.1.2 设计决策

在 `_prune_stale_focus_pools` 的清理循环中加一行：
```python
for cid in stale:
    self.focus_pools.pop(cid, None)
    self._proactive_injection_lock.pop(cid, None)  # ← 新增
```

#### 6.1.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| `gate.py` | +1 行 | +1 |

---

### 6.2 R13: `chat_state_service._chat_locks` 上限清理

**涉及文件**: `astrmai/state/chat_state_service.py`

#### 6.2.1 当前状态

```python
# chat_state_service.py:30 — 当前
self._chat_locks: Dict[str, asyncio.Lock] = {}
# 只增不减，_get_lock() 为每个新 chat_id 创建 Lock

# chat_state_service.py:35-38 — 当前
async def _get_lock(self, chat_id: str) -> asyncio.Lock:
    if chat_id not in self._chat_locks:
        self._chat_locks[chat_id] = asyncio.Lock()
    return self._chat_locks[chat_id]
```

#### 6.2.2 设计决策

**LRU 上限策略**（保守方案，避免探测锁状态）：
```python
MAX_CHAT_LOCKS = 500  # 上限
_last_lock_prune: float = 0.0

async def _get_lock(self, chat_id: str) -> asyncio.Lock:
    # 周期性清理
    now = monotonic()
    if len(self._chat_locks) > MAX_CHAT_LOCKS and now - self._last_lock_prune > 300:
        # 保留最近访问的 300 个
        # (简单策略: dict 本身是 insertion-ordered in Python 3.7+)
        excess = len(self._chat_locks) - 300
        keys_to_remove = list(self._chat_locks.keys())[:excess]
        for key in keys_to_remove:
            self._chat_locks.pop(key, None)
        self._last_lock_prune = now
    
    if chat_id not in self._chat_locks:
        self._chat_locks[chat_id] = asyncio.Lock()
    return self._chat_locks[chat_id]
```

#### 6.2.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| `chat_state_service.py` | +10 行清理逻辑 | +10 |

---

### 6.3 R14: `memory_engine._disabled_cognitive_feedback_keys` TTL 重构

**涉及文件**: `astrmai/memory/services/memory_engine.py`

#### 6.3.1 当前状态

```python
# memory_engine.py:86 — 当前
self._disabled_cognitive_feedback_keys: set[tuple[str, str, str, str]] = set()

# memory_engine.py:353 — 当前
def disable_cognitive_feedback(self, signal):
    self._disabled_cognitive_feedback_keys.add(self._cognitive_feedback_key(signal))

# memory_engine.py:471 — 当前
if self._cognitive_feedback_key(signal) in self._disabled_cognitive_feedback_keys:
    return  # 跳过此信号
```

#### 6.3.2 设计决策

**重构为 `dict[str, float]`（key → 禁用时间戳）+ TTL 清理**：
```python
# memory_engine.py:86 — 改为
self._disabled_cognitive_feedback_keys: dict[str, float] = {}
DISABLE_TTL_SEC = 7 * 86400  # 7 天

# memory_engine.py:353 — 改为
def disable_cognitive_feedback(self, signal):
    now = time.time()
    key = self._cognitive_feedback_key_str(signal)
    self._disabled_cognitive_feedback_keys[key] = now
    # 惰性清理
    stale = [k for k, ts in self._disabled_cognitive_feedback_keys.items() if now - ts > DISABLE_TTL_SEC]
    for k in stale:
        del self._disabled_cognitive_feedback_keys[k]

# memory_engine.py:471 — 改为
key = self._cognitive_feedback_key_str(signal)
if key in self._disabled_cognitive_feedback_keys:
    return

# 新增辅助方法
def _cognitive_feedback_key_str(self, signal) -> str:
    return f"{signal.chat_id}|{signal.source}|{signal.summary}|{signal.guidance}"
```

#### 6.3.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| `memory_engine.py` | 类型重构 + 清理逻辑 | +15 / -5 |

---

### 6.4 R15: `private_chat_manager._chat_to_user` 随会话清理

**涉及文件**: `astrmai/state/private_chat/private_chat_manager.py`

#### 6.4.1 当前状态

```python
# private_chat_manager.py:26 — 当前
self._chat_to_user: dict[str, str] = {}  # chat_id → user_id

# private_chat_manager.py:192 — _bind_chat_session 添加映射
self._chat_to_user[chat_id] = user_id

# private_chat_manager.py:cleanup_stale_sessions — 只清理 _sessions，不清理 _chat_to_user
```

#### 6.4.2 设计决策

在 `cleanup_stale_sessions()` 末尾加清理：
```python
# cleanup_stale_sessions — 加在末尾
for chat_id in closed_chat_ids:
    self._chat_to_user.pop(chat_id, None)
```

在 `close_session()` 中同步清理：
```python
async def close_session(self, user_id: str):
    # ... existing logic ...
    # 清理反向映射
    to_remove = [cid for cid, uid in self._chat_to_user.items() if uid == user_id]
    for cid in to_remove:
        self._chat_to_user.pop(cid, None)
```

#### 6.4.3 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| `private_chat_manager.py` | +4 行 | +4 |

---

## 7. Wave 5 — P2 测试基础设施（R16–R18）

### 7.1 R16: 测试 mock 同步 `time.monotonic`

**涉及文件**: `tests/unit/conversation/test_group_dialogue_store_and_compaction.py` 等

#### 7.1.1 当前状态

Phase 5 将 23 个文件的 `time.time()` 替换为 `time.monotonic()`，但测试中对 `time.time` 的 mock 未同步更新：
```python
# 测试中 — 当前
@patch("time.time")
def test_compaction_cooldown(self, mock_time):
    mock_time.return_value = 1000000.0
    # 但代码中已改为 time.monotonic() — mock 无效！
```

#### 7.1.2 设计决策

**两步排查 + 逐文件更新**：
1. `grep -r "time\.time\|monotonic" tests/` → 列出所有涉及时间 mock 的测试
2. 对被测试代码已改为 `monotonic()` 的，更新 `@patch` target
3. 对被测代码**未**改的（DB 边界），保留原 `@patch("time.time")`

```python
# 修复前
@patch("time.time")
@patch("astrmai.conversation.attention.context_compaction.time.time")  # ← 模块级 mock
def test_something(self, mock_time):
    ...

# 修复后 — 如果被测代码已用 monotonic:
@patch("time.monotonic")
def test_something(self, mock_monotonic):
    mock_monotonic.return_value = 1000000.0
    ...
```

#### 7.1.3 影响范围

| 类别 | 估计数量 |
|------|:--:|
| 需更新 `@patch("time.time")` → `@patch("time.monotonic")` | ~25 |
| 需更新 `mock_time.return_value` → `mock_monotonic.return_value` | ~25 |
| 不需更新（DB 边界，仍用 `time.time`） | ~5 |

---

### 7.2 R17: `safe_create_task` 单元测试（新建）

**涉及文件**: 新建 `tests/unit/shared/test_safe_create_task.py`

#### 7.2.1 设计决策

```python
import asyncio
import pytest
from unittest import mock
from astrmai.shared.helpers.plugin_helpers import safe_create_task

class TestSafeCreateTask:
    @pytest.mark.asyncio
    async def test_normal_completion_no_error_log(self):
        """正常完成不触发 error 日志"""
        async def ok(): return 42
        with mock.patch("astrmai.shared.helpers.plugin_helpers._astrbot_logger") as mlog:
            task = safe_create_task(ok())
            result = await task
            assert result == 42
            mlog.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_triggers_error_log(self):
        """异常触发 error 日志"""
        async def fail(): raise ValueError("test error")
        with mock.patch("astrmai.shared.helpers.plugin_helpers._astrbot_logger") as mlog:
            task = safe_create_task(fail())
            with pytest.raises(ValueError):
                await task
            mlog.error.assert_called_once()

    def test_returns_task_object(self):
        """返回 asyncio.Task"""
        async def ok(): pass
        task = safe_create_task(ok())
        assert isinstance(task, asyncio.Task)
```

#### 7.2.2 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| `tests/unit/shared/test_safe_create_task.py` | 新建 | +40 |

---

### 7.3 R18: Hook 异常韧性测试

**涉及文件**: 新建 `tests/unit/test_hook_error_resilience.py`，修改 `tests/test_main_reverse_session_hook_refactor.py`

#### 7.3.1 设计决策

```python
# tests/unit/test_hook_error_resilience.py — 新建
class TestHookErrorResilience:
    @pytest.mark.asyncio
    async def test_inject_reverse_session_handles_internal_error(self):
        """Hook 内部异常不传播到框架"""
        plugin = create_mock_plugin()
        event = create_mock_event()
        request = create_mock_request()
        
        # 模拟 maybe_attach_reverse_session_block 抛出异常
        with mock.patch("main.maybe_attach_reverse_session_block", side_effect=RuntimeError("boom")):
            # 不应抛出异常
            await plugin.inject_gemini_reverse_session(event, request)
            # 验证：事件流未被阻断
            assert not event.stop_event_called

    @pytest.mark.asyncio
    async def test_sniff_external_results_handles_error(self):
        """sniff hook 内部异常不传播"""
        ...

    @pytest.mark.asyncio
    async def test_intercept_errors_handles_error(self):
        """intercept hook 内部异常不传播"""
        ...
```

#### 7.3.2 影响范围

| 文件 | 改动 | 行数估计 |
|------|------|:------:|
| `tests/unit/test_hook_error_resilience.py` | 新建 | +60 |
| `tests/test_main_reverse_session_hook_refactor.py` | 修正导入 | ±5 |

---

---

## 8. Risk Assessment

| 风险 | 等级 | 触发条件 | 缓解措施 |
|------|:--:|------|------|
| **日志膨胀** — Wave 1 的 `logger.exception()` 在异常高频场景下可能刷爆日志 | 🟡 | 某链路持续失败，每秒产出一条堆栈 | 在 `logger.exception` 前加 `exc_info=True` 而非全量 exception（用 `logger.warning(msg, exc_info=True)` 替代），堆栈仅在第一次出现时完整输出 |
| **DB 查询截止时间钳制过激** — Wave 3 将 `cutoff` 钳制为 `0.0` 会查全量数据 | 🟡 | NTP 回拨 > 数据保留窗口 | 钳制为 `0.0` 是安全回退：宁可查全量（性能差）也不丢数据 |
| **`_chat_locks` FIFO 清理误删活跃锁** — 简单的 insertion-order 清理可能删掉活跃 chat 的锁 | 🟡 | 老 chat 仍在活跃但排在 dict 前面 | 改用「最近被访问」策略：在 `_get_lock` 中将访问的 key 移到 dict 末尾（`self._chat_locks[chat_id] = self._chat_locks.pop(chat_id)`） |
| **测试 mock target 路径变更遗漏** — `grep` 可能漏掉动态 mock | 🟢 | 用了 `mock.patch.object` 或 `monkeypatch` | 在 CI 中运行完整测试套件，按失败列表逐一修复 |
| **`_conf_schema.json` 字段类型不匹配** — WebUI 传入的 JSON 值与 pydantic 类型不一致 | 🟢 | 某字段为 `int` 但 WebUI 传了 `"60"` (str) | pydantic 自带类型转换，`int` 字段接受 `"60"` 字符串 |
| **`safe_create_task` 在无事件循环时抛异常** — 测试中未 mock 事件循环 | 🟢 | 测试未用 `@pytest.mark.asyncio` | 单元测试中显式 mock 或使用 `pytest-asyncio` |

## 9. Verification Matrix

| 需求 | 验证方式 | 通过标准 | 验证文件 |
|------|---------|---------|---------|
| R1 | `grep -c "logger.exception\|logger.warning"` 变更前后对比 | 新增 ≥ 48 处日志调用 | 全量变更文件 |
| R2 | 人工检查 `gate.py` 4 处 | 每 `except Exception:` 后有 `logger.warning/exc_info` | `gate.py` |
| R3 | 人工检查 `persona_summarizer.py` 8 处 | 每处含切片标识字符串 | `persona_summarizer.py` |
| R4 | `pytest tests/ -k "memory or state"` | 无新增失败 | `tests/` |
| R5 | `python -c "from astrmai.config import ConversationConfig; assert hasattr(ConversationConfig, 'enable_token_estimator')"` | Import 无异常，属性存在 | `config.py` |
| R6 | `python -c "from astrmai.config import EvolutionConfig; assert hasattr(EvolutionConfig, 'review_runner_interval_sec')"` | 同上 | `config.py` |
| R7 | `python -c "from astrmai.config import MemoryConfig; assert hasattr(MemoryConfig, 'auto_recall_probability')"` | 同上 | `config.py` |
| R8 | 手动 diff 对齐清单 | 0 差异 | `config.py` + `_conf_schema.json` |
| R9 | `grep "max(0.*delta\|cutoff.*now.*clamp"` 4 站点 | 全部包含钳制逻辑 | 4 文件 |
| R10 | `grep "max(0.0, time.time()"` 3 站点 | 全部包含 `max(0, ...)` | 3 文件 |
| R11 | `grep -c "ponytail: wall-clock"` | ≥ 7 处注释 | 7 文件 |
| R12 | `grep "_proactive_injection_lock.pop" gate.py` | 出现在 `_prune_stale_focus_pools` 内 | `gate.py` |
| R13 | 启动后发送 > 500 个不同 chat_id 的消息 → 检查 `len(_chat_locks)` | ≤ 500 | `chat_state_service.py` |
| R14 | 运行 8 天后检查 `len(_disabled_cognitive_feedback_keys)` | 增长趋缓（TTL 7 天生效） | `memory_engine.py` |
| R15 | 关闭私聊后检查 `len(_chat_to_user)` | 关闭的 chat 不在 dict 中 | `private_chat_manager.py` |
| R16 | `pytest tests/ -q` | 失败数从 59 降至 ≤ 5（排除预存） | `tests/` |
| R17 | `pytest tests/unit/shared/test_safe_create_task.py -v` | 3/3 passed | 新建测试文件 |
| R18 | `pytest tests/unit/test_hook_error_resilience.py -v` | ≥ 3/3 passed | 新建测试文件 |

### 汇总

| 指标 | 值 |
|------|:--|
| 总变更文件数 | ~40（含测试） |
| 总代码行数变化 | ~+150 / -10 |
| 新增测试文件 | 2（`test_safe_create_task.py`、`test_hook_error_resilience.py`） |
| 最高风险 | 🟡 — 日志膨胀、DB 查询全量 |
| 最低风险 | 🟢 — 配置同步、注释标注 |

---
_（design.md — 写入 3/3 完成。18 条需求 × 5 Wave 模块设计，Spec Phase 2 结束。）_

