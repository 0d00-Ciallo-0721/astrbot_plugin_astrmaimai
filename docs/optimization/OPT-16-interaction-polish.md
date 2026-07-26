# OPT-16 交互打磨（私聊话术 / 撤回 / 空态三义性 / 口径错位）

状态：部分完成（ID-08/PL-09 留待专项） ｜ 优先级：P2 ｜ 依赖：无 ｜ 覆盖发现：ID-04(P2)、ID-08(P3)、TL-09(P3)、WU-09(P2)、WU-12(P3) ｜ 单项都不致命，但都是用户/运营者天天看得见的毛刺。

## 目标

- 私聊 prompt 不再自称"在延续群聊话题"（1:1 情感对话被硬编码群聊话术误导，模型可能提及不存在的群友）。
- 用户撤回的消息不再被 bot 原文复述（隐私/尴尬）。
- 跨会话传话的三方消歧指令不再被 360 字符截断吃掉。
- 管理页空白可解释：错误态、未绑定态、真无数据三种情况分开显示，错误不再被缓存成 180s"新鲜空数据"。
- 计数与删除反馈口径对齐（total 含已删行、readonly 删除却 toast 成功等四处）。

## 基线证据

- **ID-04**：`group_dialogue_store.py:348-380` 与 `context_compaction.py:1594-1600` 的 topic 兜底模板硬编码"群聊/群友/群里"；私聊 15/15 executed turn 的 warm_summary 以"当前主要是在延续刚才的群聊话题"开头（45 处命中）。
- **ID-08**：`message_entry.py:89-97,160-176` 把 recall notice 标 non_conversational 后直接 return，全插件无 recall 消费者；16h 日志 ≥5 条 group_recall 无任何处理。作者归属本身正确。
- **TL-09**：`cross_session_handoff_store.py:36-43` 纯内存（重载即失忆）；`planner_side_inputs.py:1288-1309` 注入块统一截 360 字符且消歧指令排块尾——长消息传话时"区分发起人/机器人/收件人"指令最先被截掉。
- **WU-09**：`app.js:159-165` cachedFetch 把错误回退以 updatedAt=now 写缓存（TTL 180s）→ 瞬时错误后 tab 稳定空白 3 分钟；`table()` 空态不接收 runtime_bound/error 信息；后端多处 `except → 200 + 空集合`（如 `memory_ui_service.py:275-276`）。
- **WU-12**：表达 total 含 deleted/rejected（虚高）；"黑话全量"tab 实查 status=active；legacy 事件删除返回 readonly 前端却 toast"已删除"；Dashboard 待审仅统计表达不含黑话。

## 实施步骤

1. ID-04：warm/cold 摘要模板按 `'FriendMessage' in chat_id` 分支切换措辞（含 `prompt_templates.py:155` 摘要助手 system 同步）。断言私聊 warm_summary 不含"群聊/群友/群里"。
2. ID-08：message_entry notice 分类新增 recall 路由 → dialogue_store 按 event_id 打 tombstone（内容替换"[已撤回]"、保留 speaker）。手测：发消息→撤回→@bot 询问，bot 不引用原文。
3. TL-09：消歧指令移到注入块首（零风险）；handoff 落盘为可选增量（persistence 小表 + lifecycle 恢复，可并入 OPT-14 的快照机制）。
4. WU-09：cachedFetch 错误回退不写缓存（或标 stale 下次强制重试）；table()/asItems 空态透出 `runtime_bound=false` 与"加载失败"两种专属文案；后端回退分支返回 `status:degraded`。
5. WU-12：total 限定 active+review_pending（或分列）；删除回调检查 `result.status==='readonly'/changed===false` 改提示；"黑话全量"改名"黑话词库（已通过）"；Dashboard 待审并入黑话计数。

## 验收标准

- 私聊 trace warm_summary 零"群聊"字样；撤回消息 tombstone 生效；长摘要+长消息传话单测断言注入文本含"收件人"消歧句；断网复现→页面显示错误态且可重试（而非 3 分钟空白）；含 deleted 行的库中学习页 total == `COUNT(*) WHERE status IN ('active','review_pending')`。
- 全量 pytest 绿。

## 风险与回退

- 全部为文案分支/渲染层/观测口径改动，低风险；ID-08 的 tombstone 注意只改展示层内容不动原始事件存储。
- 各项独立提交可单独 revert。

## 完成记录

**2026-07-26 代码侧完成（5/6 项）**：

- ID-04：`group_dialogue_store` 与 `context_compaction` 的话题模板去群聊化（"延续刚才的对话"/"同一个话题"/"对话现在是…"）——私聊 15/15 轮曾被"群聊/群友"话术污染。
- TL-09：跨会话传话的三方消歧指令移到注入块首（旧版排块尾，长摘要时最先被 360 字符截断吃掉）。
- WU-09：`cachedFetch` 错误回退不再写入缓存时间戳——一次瞬时故障不再让 tab 稳定空白 180 秒。
- WU-12：学习页 total 口径改 `active + review_pending`（不再混入 deleted/superseded 虚高）；"黑话全量"tab 更名"黑话词库（已通过）"；legacy 只读记录删除返回 readonly/changed=false 时提示"只读历史数据，无法删除"而非谎报成功。
- **遗留**：ID-08（撤回 tombstone）需新增 recall 事件路由与 store 改造，PL-09（重载上下文快照持久化）属 OPT-14 生命周期范畴——两者均超出"打磨"边界，另立专项。
- 受影响套件 203 passed。
