# OPT-10 配置真源与容错（死键清理 / 降级加载）

状态：未开始 ｜ 优先级：P1 ｜ 依赖：无 ｜ 覆盖发现：PL-03(P1)、PL-04(P1)、PL-05(P2)、PL-06(P2)、PL-11(P3) ｜ 全量矩阵见 `../../.agent/claude-full-audit-20260727/config_consumption_matrix.md`（209 键：死键 9、pydantic-only 12、getattr 第三层漂移 11、仅 25 键有 UI 范围提示）。

## 目标

- UI 上每个配置项要么真实生效、要么不存在：清掉 9 个死键（含 1 个虚假安全开关），修好 1 个挂错分节的死开关。
- 越界配置从"整个插件拒载"变为"裁剪+告警+降级加载"。
- `agent.max_steps` 等静默钳制行为与 UI 声明一致。

## 基线证据

- **PL-03**：schema 把 `turn_merge_enabled` 挂在 `timing.items`（L1095），pydantic 定义在 `PrivateChatConfig`（config.py:293），`LEGACY_TIMING_NAMESPACE_FIELDS` 未收录该键 → UI 写入被 `extra=ignore` 静默丢弃。运行时实证：`AstrMaiConfig(**{'timing':{'turn_merge_enabled':False}})` 后 `private_chat.turn_merge_enabled == True`。
- **PL-04**：`enable_content_safety_filter`（config.py:179 + schema L433）全仓库零消费点，NSFW/自残/PII 检测代码不存在——运营者开启后以为有兜底，实际所有输出直发。
- **PL-05**：另 7 个死键（debounce_window/max_message_length/repeater_threshold/throttle_probability/throttle_min_entropy/enable_relationship_engine/unknown_decay）——防抖硬编码分档、复读阈值硬编码 3、限流改能量驱动、RelationshipEngine 无条件实例化。
- **PL-06**：`main.py:62-65` `AstrMaiConfig(**raw_config)` 无 try/except；本地实测 `api_timeout=-5`/`bg_pool_size=0`/`meme_probability='abc'` 全部 ValidationError → **整个插件下线**；约 90 个数值键 pydantic 有界但 UI 无提示；插件自有 WebUI 的 `apply_config`（plugin_api.py:458-471）反而会优雅拒绝——两条路径行为不一致。
- **PL-11**：`executor.py:529-531` `max_steps = max(5, config_max_steps)` 与配置声明 ge=1 矛盾，1-4 设置无效。

## 实施步骤

1. PL-03：`LEGACY_TIMING_NAMESPACE_FIELDS` 增加 `('turn_merge_enabled','private_chat','turn_merge_enabled')` 并在 TimingConfig 增加该字段。测试：timing 写 False → private_chat 读到 False。
2. PL-04：**二选一（需产品拍板）**——(a) 从 schema+config.py 删除该键并在变更说明标注（推荐，诚实优先）；(b) 在 reply_service/output_guard 实现最小过滤并接开关（需误杀率评估）。
3. PL-05：7 键逐个决策"删除 or 接回逻辑"（repeater_threshold 若接回需保持默认 3 条行为），默认删除；复跑 config_matrix 脚本确认 ①类清单归零。
4. PL-06：`main.py __init__` 捕获 ValidationError → 剔除违例字段回退默认 + `logger.error` 逐项汇总（对齐 plugin_api.apply_config 的语义）；长期为 schema 补齐 min/max（先补 timing/预算/概率类高危键）。合同测试："坏配置应降级加载而非拒载"。
5. PL-11：尊重配置或把 pydantic/schema 下限提到 5 并更新 hint（推荐后者——5 是有意的安全下限，改声明比改行为稳）。
6. 新增静态守卫测试：schema 每个叶子键都被 pydantic 接受且映射到有效字段（防再犯 PL-03 类挂错分节）。

## 验收标准

- `AstrMaiConfig(**{'timing':{'turn_merge_enabled':False}}).private_chat.turn_merge_enabled is False`；注入 `{'infra':{'api_timeout':-5}}` 实例化插件成功且日志含降级警告；config_matrix 复跑死键=0；schema-pydantic 一致性测试绿；全量 pytest 绿。
- UI 手测：私聊合并开关真实生效（关闭后连续两条私聊分别回复）。

## 风险与回退

- PL-06 **中风险**：降级加载需保证剔除字段后的组合仍自洽（timing 别名连带校验）——合同测试覆盖别名链；极端情况下回退整体默认配置并大写告警。
- PL-04 若走实现路径为中风险（误杀）；删除路径低风险。
- 死键删除对老配置文件无破坏（多余键本就被 ignore）。
- 各项独立提交可单独 revert。

## 完成记录

（完成后填写：死键处置清单、降级加载测试输出、schema min/max 补齐范围）
