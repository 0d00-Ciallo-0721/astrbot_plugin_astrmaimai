# OPT-12 工具链路修复（图片轮工具可见性 / 披露死路 / 触发词误伤）

状态：代码完成 ｜ 优先级：P2 ｜ 依赖：无 ｜ 覆盖发现：TL-02(P2)、TL-03(P2)、TL-07(P2)、TL-01(P2)、TL-06(P2/LIKELY)、TL-08(P3) ｜ 工具系统的观测无缺口（trace 空是因为模型真不调工具），失败面集中在三处接缝。

## 目标

- 图片消息在任何 social_intent 下都保有查图工具；vision/artifact 工具的"当前消息"路径不再必然假阴性（当前实测一轮浪费 4 次工具调用 21.5s 后答"图好像还在加载中"）。
- 二段披露从"585 轮零触发的死路"变为可达（或明确降级为直接并包）。
- "听说/据说"等日常词不再强制触发 unverified_report 工具轮。
- perception.image_count 从恒 0 修为真实值（图片轮可观测）。

## 基线证据

- **TL-02**：`planner_side_inputs.py:1005-1007` 仅 `intent_families is None` 时才把披露家族并入白名单；tease/comfort 的 fun 家族随后把披露层刚为图片轮加的 artifact/core/wait 工具**全部滤除**（trace 1785050973 实证：filtered_tools 只剩 meme/表情四件）。
- **TL-03**：`executor.py:106-114` sanitized event 把 `message_obj.message` 整体替换成 `[Plain(safe_text)]`；pfc 工具经 `_get_current_event` 拿到的正是它 → 首调必返"没有发现可分析的图片"（日志 06:12:40 实锤，需模型自己猜 message_id 走 NapCat get_msg 兜回）。
- **TL-07**：`perception.image_count` 585/585 恒 0（disclosure 用别的来源判 has_image 为真），图片轮在 trace 层不可辨识。
- **TL-01**：二段披露唯一入口是模型主动调 `bot_capability_lookup`（16h 0 次、65/68 工具轮 0 工具调用），触发后还要整轮重跑——机制死路；62/70 轮仅披露 core 6 件套。
- **TL-06**（LIKELY）：`GENERAL_EXPLICIT_TOOL_KEYWORDS['unverified_report']` 触发词过于日常（听说/据说/有人说/不确定），命中即 required 强制工具轮；16h 未踩中但触发面大。
- **TL-08**：`FAMILY_TO_PACKAGES['quote_reply']` 是死配置（quote_reply 属 PRECISION_ONLY 被剔除，映射永不生效），三表矛盾。

## 实施步骤

1. TL-02：`_build_execution_tools` 把 message_artifact/vision_message（及 wait）家族并入 intent_families 白名单，或 disclosure_reasons 含 artifact 类时保护对应工具（类似 explicit_qq_action_restore 的既有保护逻辑）。测试：has_image=True + social_intent=tease 断言 vision_message_analyze_tool 在 filtered_tools。
2. TL-03：sanitized event 上以 extra（`astrmai_original_message_segments`）保留原始组件供**只读工具**解析（不回流 prompt，保住防注入语义）；pfc 当前消息分支优先读该 extra。测试：带 Image 段事件 sanitize 后 VisionMessageAnalyzeTool 返回图片段信息。
3. TL-07：perception 装配点用与 disclosure 相同的来源为 image_count 赋值；一致性断言测试。
4. TL-01：`_append_tool_guidance` 增加"工具不够时调用 bot_capability_lookup(needed_package=…)"提示；同时在识别到 identity/relationship 疑问信号但关键词未命中时直接并包（弱化模型自检依赖）。部署后统计 expanded 使用率与 identity 工具执行次数由 0 转正。
5. TL-06：先跑一周线上 trace 统计触发率（LIKELY 取证）；然后触发词收紧为组合模式（转述源+断言结构）或该家族从 required 降级 optional。负样本测试："我听说你会画画"不产生 required 计划。
6. TL-08：删除 PRECISION_ONLY 家族在 FAMILY_TO_PACKAGES 的映射 + 三表一致性单测（`PRECISION_ONLY ∩ 有效包映射 = ∅`）。

## 验收标准

- 上述单测全绿 + 全量 pytest 绿。
- 部署复采：图片轮（image_count>=1 且可辨识）filtered_tools 含 vision 工具；vision 工具首调命中率恢复（不再"没发现图片"）；disclosure_expanded_packages 非空次数 >0；无"听说"类误触发 required 轮。

## 风险与回退

- TL-02 中风险：扩大 tease/comfort 轮工具面（均只读，风险有限）。
- TL-03 中风险：保留原始组件需确保只供只读工具解析、不回流 prompt——测试锚定。
- TL-01 低风险（guidance 文本+只读包）；TL-06 收紧仅影响该家族精度（真实纠错另有 memory_correction 家族）。
- 各项独立提交可单独 revert。

## 完成记录

**2026-07-26 代码侧完成**：

- TL-02：`planner_side_inputs` 在 intent 家族白名单分支新增**保护家族**（message_artifact/vision_message/quote_reply/topic_thread/wait/capability）——披露层为图片/引用轮特意加的只读查证能力不再被 tease/comfort 静默剥除。
- TL-03：`executor._build_sanitized_execution_event` 以 `astrmai_original_message_segments` extra 保留原始组件；`pfc_tools` 视觉工具当前消息分支优先读该 extra（sanitize 后首调必答"没有发现图片"的假阴性消除，不回流 prompt 保住防注入语义）。
- TL-07：`gate.process_event` 填充 `turn_context.perception.image_urls`（trace image_count 派生自它，585/585 恒 0 使图片轮不可辨识）。
- TL-01：`planner._append_tool_guidance` 在工具集含 `bot_capability_lookup` 时追加自检提示（二段披露 16h 零触发的唯一入口从未被提示过）。
- TL-06：`unverified_report` 触发词由日常词（听说/据说/不确定）收紧为"记录一下听说/登记未核实"等明确登记意图组合。
- TL-08：删除 `FAMILY_TO_PACKAGES["quote_reply"]` 死映射（PRECISION_ONLY 家族在 plan() 中被剔除，映射永不生效且与 TOOL_PACKAGES 矛盾）。
- 既有契约更新 1 处：`test_agency_tier_none_and_social_intent_constrain_tools` 的 comfort 期望集合补入 wait/capability（旧断言锁定的正是"连等待与自检能力一并清空"）。
- 受影响套件 184+70 passed。

### G5 补充（2026-07-26）：TL-01 后半 —— 语义意图直接并包

OPT-12 只做了 guidance 提示（告知模型可调 `bot_capability_lookup` 自检）；二段披露 16h 零触发
说明不能只依赖模型自检。本次补齐"识别到信号即直接并包"。

- `tool_disclosure.plan()`：关键词未命中时用 `QueryIntentClassifier`（与 OPT-06 记忆检索门**同一个
  分类器**，两处对身份类问句判定一致）兜底并包，reason 标 `<package>_semantic_intent` 与
  关键词命中的 `_signal` 区分开；关键词已命中时不重复并包。
- **映射决策（含一次纠错）**：初版把 `recent_reference → relationship`，被既有
  `test_agency_tier_none_and_social_intent_constrain_tools` 抓出——"你还记得我之前说的吗"是
  **记忆回想**，并包 `qq_friend_lookup`/`contact_route_suggest_tool` 等联系人路由工具属语义错配。
  最终只保留 `identity/location → identity`（"查得到答案"的意图），记忆回想交给 OPT-06 的注入链路。
  该决策已写成负向断言固化。
- 测试：`tests/regression/conversation/test_semantic_tool_disclosure.py` 6 条（红验证 3 红），
  含闲聊/空消息保持 core-only 的负向对照；既有 49 项工具相关测试**无需修改**即通过。

全量回归 **1811 passed, 1 skipped**。
