# 开发窗口 12：最终收口 — 剩余债务 + 测试修复

## 必须先读取的审查报告
1. `artifacts/reviews/r12-remaining-debt.md` — 剩余待修完整清单
2. `artifacts/reviews/r15-master.md` — 总报告
3. `artifacts/reviews/r13-session-fixes.md` — 本轮修复记录（确认哪些已修）
4. 窗口 01-11 各审查报告的 🟡/🟢 部分（处理非严重项）
5. `artifacts/reviews/r14-index.md` — 旧版索引

## 目标
1. 修复剩余的 12 个预存测试失败
2. 处理各窗口的 🟡/🟢 建议项
3. 确认延期项清单

## 当前测试基线
692 passed / 12 failed / 1 skipped

---

## 一、预存测试失败（12 项）

### Mood Chain 审计漂移（2 项）
| # | 测试 | 文件 | 根因 |
|---|------|------|------|
| T4 | `test_host_message_entry_matches_direct_mood_result` | `tests/test_host_mood_chain_audit_refactor.py:32` | `drift_detected` |
| T5 | `test_host_reply_post_send_matches_expected_mood_and_social_score` | `tests/test_host_mood_chain_audit_refactor.py:43` | `drift_detected` |
- **修复**：更新 `tests/helpers/host_mood_chain_audit.py` 中的基准数据，或确认实现变更是否为预期

### Ported 测试（5 项）
| # | 测试 | 文件 | 根因 |
|---|------|------|------|
| — | `test_behavior_rules_change_with_reply_mode_and_freshness` | `tests/original_ported/test_context_behavior_rules_ported.py:16` | `ContextEngine` 缺 `_build_behavior_rule_block` |
| — | `test_selector_cools_down_recent_patterns_and_filters_short_repeats` | `tests/original_ported/test_expression_selector_reviewed_ported.py` | 空字符串断言 |
| — | `test_selector_passes_review_filters_and_scope` | 同上 | 空字符串断言 |
| — | `test_refiner_prefers_prompt_envelope_when_available` | `tests/original_ported/test_prompt_envelope_rendering_ported.py` | prompt 渲染基线漂移 |
| — | `test_p2_99_acceptance_docs_exist` | `tests/regression/architecture/test_directory_contracts_refactor.py` | docs 目录缺失 |

### Profile Roundtrip（2 项）
| # | 测试 | 文件 | 根因 |
|---|------|------|------|
| T12 | `test_get_user_profile_does_not_rebuild_runtime_vector_from_social_score` | `tests/unit/state/test_relationship_profile_roundtrip_migrated.py:61` | `KeyError: 'trust'` |
| T13 | `test_relationship_vector_roundtrip_preserves_last_decay_time` | 同上:136 | `KeyError: 'relationship_vector'` |

### Schema 迁移（3 项）
| # | 测试 | 文件 | 根因 |
|---|------|------|------|
| T9 | `test_chat_state_roundtrip_preserves_decay_fields` | `tests/unit/state/test_chat_state_persistence_migrated.py:61` | `no column: last_reply_time` |
| T10 | `test_database_service_get_chat_state_preserves_decay_fields` | 同上:87 | 同上 |
| T11 | `test_get_state_persists_daily_reset_on_first_load` | 同上 | 日期断言失败 |
- **注意**：T9-T11 应在窗口 02（Infrastructure）中修复，此处仅验证

---

## 二、各窗口 🟡/🟢 建议项处理

详见各审查报告（r01-r11），优先级：
1. 低风险高收益项（docstring、类型标注、魔法数字替换）
2. 代码整洁项（方法提取、重复代码消除）
3. 架构改进项（标记为独立窗口）

---

## 三、确认延期（6 项 — 不阻塞）

| 项 | 优先级 | 说明 |
|----|--------|------|
| context_compaction.py 拆分 | 🔴 P1 | 1999 行单体 |
| D27 _get_runtime() 迁移 | 🟡 P2 | 跨 10+ 文件 |
| D24/D25 bootstrap 提取 | 🟡 P2 | 双向绑定阻碍 |
| D18 剩余 4 个 cache reason | 🟢 P3 | 跨数据流 |
| admin_ui_service God Object 完整拆分 | 🟡 P2 | 7 个领域服务 |
| T9-T11 DB migration | 🟡 P2 | 窗口 02 处理 |

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/ -q --tb=line
```

## 成功标准
- 12 → 0 失败（或全部标记为已知延期）
- 692+ passed
