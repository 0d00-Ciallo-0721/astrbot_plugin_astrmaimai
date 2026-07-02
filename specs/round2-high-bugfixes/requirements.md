# Requirements Document — Round 2 HIGH Bugfixes

## Introduction

本 Spec 为 AstrMai 第二轮深度审计中发现的 **13 个 HIGH 严重等级**缺陷制定修复需求。全部为生产环境中会触发的高影响 bug。

## Glossary

- **TOCTOU**: Time-of-check-to-time-of-use 竞态条件
- **RRF**: Reciprocal Rank Fusion，混合检索融合算法
- **CAS**: Compare-and-Swap，乐观锁模式
- **UNIQUE约束**: SQL 唯一性约束，防止重复行
- **PRAGMA user_version**: SQLite 内置迁移版本号

## Requirements (Wave 1: 全部 13 项，可并行)

| # | 标题 | 影响 |
|---|------|------|
| R1 | dream_interval_min ge=1 | 配置为0时无限触发梦境 |
| R2 | dream 节流加锁 | 并发梦境绕过节流 |
| R3 | attention_gate=None → 日志+通知 | 消息静默丢失 |
| R4 | breaker 统一 monotonic() | breaker 永久失效 |
| R5 | think_level 移除 ≤4 字符规则 | 短消息被误判 |
| R6 | ExpressionPattern 添加 UNIQUE 约束 | 重复行竞态 |
| R7 | 异步 _init_db 更新 user_version | 每次重启重跑migration |
| R8 | TimeoutError 不被外层捕获 | 超时被误判致命 |
| R9 | cooldown 统一 monotonic() | 冷却期不可预测 |
| R10 | BM25 单结果 score 不归零 | 单结果被淘汰 |
| R11 | VisualCortex queue 加 maxsize | OOM |
| R12 | PROACTIVE_BLOCKED 消息排队重试 | 用户消息丢弃 |
| R13 | startup_hooks 启动失败 raise | 半初始化运行 |

---

### R1: dream_interval_min 下限设为 1
**AC**: `ge=0` → `ge=1`。`_conf_schema.json` 同步更新 hint。  
**文件**: `config.py:122`, `_conf_schema.json`

### R2: 梦境节流在信号量内检查
**AC**: `_run_for_session` 中 `async with self._bg_semaphore:` 代码块内进行 `should_run` 检查。  
**文件**: `dream_scheduler.py:59,79`

### R3: attention_gate 缺失时日志警告
**AC**: `message_handler=None` 时输出 `logger.warning`。  
**文件**: `bootstrap.py:357-358`

### R4: breaker 统一使用 monotonic()
**AC**: `breaker_until > monotonic()`。  
**文件**: `executor.py:211`

### R5: 移除 `len(compact_text) <= 4` 规则
**AC**: `_is_short_ack` 仅检查 `SHORT_ACKS` 集合。  
**文件**: `think_level_policy.py:202-203`

### R6: ExpressionPattern 添加复合 UNIQUE
**AC**: ORM 添加 `UniqueConstraint("group_id","situation","expression")`；`save_pattern` 捕获 `IntegrityError`。  
**文件**: `orm_models.py:22-44`, `database_review.py:88-102`

### R7: 异步 _init_db 调用 _run_migrations
**AC**: `_init_db()` 末尾调用 `await self._run_migrations()`。  
**文件**: `persistence_schema.py:251-384`

### R8: TimeoutError 不被重新抛出
**AC**: `except asyncio.TimeoutError` 块中直接 `break` 或标记为重试，不 `raise`。  
**文件**: `gateway_call.py:183-185`

### R9: cooldown 统一使用 monotonic()
**AC**: `"until": monotonic() + duration`。  
**文件**: `gateway_policy.py:51`

### R10: BM25 单结果 score 保持原值
**AC**: `score_range = max_score - min_score if max_score != min_score else max(abs(max_score), 1.0)`。  
**文件**: `bm25.py:108-112`

### R11: VisualCortex queue 添加 maxsize
**AC**: `asyncio.Queue(maxsize=100)`；`put_nowait` 满时 `logger.warning`。  
**文件**: `visual_cortex.py:21`

### R12: PROACTIVE_BLOCKED 消息缓存重试
**AC**: 被阻塞消息加入 `_deferred_messages` 队列（max 5）；proactive 完成时重放。  
**文件**: `gate.py:649-653`

### R13: startup_hooks 启动失败 raise
**AC**: `logger.exception` 后 `raise`。  
**文件**: `startup_hooks.py:13-16`

---

## Verification

| 层 | 方式 | 覆盖 |
|----|------|------|
| LSP | `lsp_diagnostics` 全部变更文件 | R1–R13 |
| pytest | `pytest tests/ -q` | 全量回归 |
| 源码检查 | 逐项确认代码变更 | R1–R13 |
