# P2 — 代码质量（~2h）

> 基于终审报告未修复项 | 3 项 | 🟡 中等

---

## #1 `gateway_lane.py` — 成功路径去重

**文件：** `astrmai/infrastructure/gateway/gateway_lane.py`
**范围：** `chat_in_lane_result` 行 317-363 vs `_finalize_tool_success` 行 475-533
**问题：** 两处成功路径的 artifact 追加、trace stage、event extras 约 70% 重复。
**修复：**

```python
# 提取共享方法
def _append_success_artifacts(self, event, result, economy_payload, workload_trace):
    """artifact 追加 + trace stage + event extras 的公共逻辑"""
    ...
```

**验证：** `python -m pytest tests/test_executor_refactor.py -q`

---

## #2 `bot_speaker_names` 数据源统一

**文件：**
- `astrmai/infrastructure/runtime/lane_history.py:18-25`
- `astrmai/infrastructure/gateway/gateway_result.py:170-177`

**问题：** 两处 `_bot_speaker_names` 分别读取 `self.settings.nicknames` 和 `self.config.system1.nicknames`，可能指向不同数据。
**修复：**

```python
# 提取为静态方法，接收 nicknames 作为参数
@staticmethod
def _bot_speaker_names(nicknames: list[str]) -> list[str]:
    ...
```

**验证：** `python -m pytest tests/test_gateway_policy_refactor.py -q`

---

## #3 `command_models.py` — `is_empty` 重命名

**文件：** `astrmai/presentation/dto/command_models.py:18`
**问题：** `is_empty` 实际含义是"缺少 task_query"，易被误解为"整个请求为空"。
**修复：**

```python
# 先搜索所有引用
# search_content: "is_empty" in astrmai/

# 改为 has_query（语义反转）
@property
def has_query(self) -> bool:
    return bool(self.task_query)
```

**验证：** `python -m pytest tests/ -q --tb=line -k "not (admin_full_fixture)"`
