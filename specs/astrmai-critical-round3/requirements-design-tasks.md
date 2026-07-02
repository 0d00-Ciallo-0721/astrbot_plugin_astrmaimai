# Requirements + Design + Tasks — AstrMai 第三轮 CRITICAL 修复

> Spec: `astrmai-critical-round3-20260630` | 8 项 CRITICAL | 6 文件

---

## Requirements

### Wave 1：P0 — asyncio.gather 保护（R1–R6）

| 需求 | 文件:行 | 问题 |
|------|---------|------|
| R1 | `planning_input_loader.py:103` | 3 并发 load 无 return_exceptions |
| R2 | `planning_input_loader.py:194` | 动态 *tasks 无保护 |
| R3 | `planning_input_loader.py:382` | state+profile gather 无保护 |
| R4 | `pfc_tools.py:211` | 5 并发 fetch 无保护 |
| R5 | `memory_tool_service.py:218` | 同上 5 并发 fetch |
| R6 | `planner_side_inputs.py:217` | 3 并发 load 无保护 |

#### Acceptance Criteria
- THE 6 处 `asyncio.gather()` SHALL 添加 `return_exceptions=True`。
- WHEN 单个任务失败，THE 其他任务 SHALL 继续执行。

### Wave 2：P1 — 私有 API 替换（R7–R8）

| 需求 | 文件:行 | 问题 |
|------|---------|------|
| R7 | `plugin_facade.py:357` | `_collect_descriptors` 私有 API |
| R8 | `persistence_manager.py:6` | `get_astrbot_data_path` 内部 API |

#### Acceptance Criteria
- THE `_collect_descriptors` SHALL 有 try/except ImportError + fallback。
- THE `get_astrbot_data_path` SHALL 有同等备选路径。

---

## Design & Tasks（合并）

### T1–T6: asyncio.gather 加 `return_exceptions=True`

所有改动为 2 字符：`)` → `, return_exceptions=True)`。

| 任务 | 文件 | 当前 | 修复后 |
|------|------|------|------|
| T1 | `planning_input_loader.py:103` | `asyncio.gather(agency_task, continuity_task, heartflow_task)` | + `, return_exceptions=True` |
| T2 | `planning_input_loader.py:194` | `asyncio.gather(*tasks)` | + `, return_exceptions=True` |
| T3 | `planning_input_loader.py:382` | `asyncio.gather(_get_state(), _get_profile())` | + `, return_exceptions=True` |
| T4 | `pfc_tools.py:211` | `asyncio.gather(_fetch_memory(), ...)` | + `, return_exceptions=True` |
| T5 | `memory_tool_service.py:218` | `asyncio.gather(_memory(), ...)` | + `, return_exceptions=True` |
| T6 | `planner_side_inputs.py:217` | `asyncio.gather(_load_slang(), ...)` | + `, return_exceptions=True` |

### T7: `_collect_descriptors` 加固

**文件**: `plugin_facade.py:357-373`

当前已有 try/except，重构为显式处理 ImportError：
```python
try:
    from astrbot.core.star.command_management import _collect_descriptors
    descriptors = _collect_descriptors(include_sub_commands=True)
    for desc in descriptors:
        ...
except ImportError:
    logger.debug("[AstrMai] _collect_descriptors not available, falling back to command_manager")
    # ... existing fallback ...
```

### T8: `get_astrbot_data_path` 备选路径

**文件**: `persistence_manager.py:6-20`

```python
try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
    base_path = Path(get_astrbot_data_path()) / "plugin_data" / "astrmai"
except ImportError:
    base_path = Path("data") / "plugin_data" / "astrmai"  # ponytail: fallback
    logger.warning("[AstrMai] get_astrbot_data_path not available, using relative path")
```

---

## 执行检查清单

- [ ] T1–T6: 6 处 `asyncio.gather` 加 `return_exceptions=True`
- [ ] T7: `plugin_facade.py` ImportError 处理
- [ ] T8: `persistence_manager.py` 备选路径
- [ ] 全部文件 ast.parse 通过
- [ ] `pytest tests/ -q` passed ≥ 844
