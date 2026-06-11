# 窗口9-Prompt10-注意力链空值防护与冗余清理审查报告

## 审查范围
- `astrmai/conversation/attention/compaction_providers.py`
- `astrmai/conversation/attention/gate.py`
- `astrmai/conversation/ingress/sensors.py`
- `tests/unit/conversation/test_group_dialogue_store_and_compaction.py`
- `tests/test_attention_gate_refactor.py`
- `artifacts/review_new/02-模块-M2-消息入口与感知注意力.md`

## 审查结论
- 本窗口最终无历史遗留问题。
- 审查过程中确认并修复了 compaction 记录路径对 `resolve_policy()` 的二次空值依赖，现已改为复用 helper 产出的 trace payload。
- `sensors=None` 的 wakeup 边界、`turn_context.perception` 的重复写入、以及 `sensors.py` 的冗余注释/重复导入均已收敛。

## 已确认事实
1. `policy is None` 时，compaction helper 会返回空 `kwargs` 和可记录的最小 trace，不再抛 `AttributeError`。
2. compaction 的两条摘要生成路径已改为直接记录 helper 产出的 trace，不再二次调用 `resolve_policy()`。
3. `AttentionGate._is_direct_wakeup_event()` 在 `sensors=None` 时稳定降级为 `False`，且保留显式唤醒 extras 的优先级。
4. `PerceptionBuilder.build()` 保持 `turn_context.perception` 的唯一写入点，`gate.py` 中的重复赋值已移除。

## 审查验证
- `python -m pytest tests/unit/conversation/test_group_dialogue_store_and_compaction.py`
- `python -m pytest tests/test_attention_gate_refactor.py`

## 备注
- 本窗口未发现新的未修复回归。
- 相关更新已同步到 `artifacts/review_new/02-模块-M2-消息入口与感知注意力.md`。
