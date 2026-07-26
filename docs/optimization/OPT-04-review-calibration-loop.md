# OPT-04 人工校准与审核闭环（WebUI 六连修）

状态：代码完成（待 WebUI 手测验收） ｜ 优先级：P1 ｜ 依赖：无 ｜ 覆盖发现：WU-01(P1)、WU-02(P1)、WU-03(P1)、WU-05(P2)、WU-07(P2)、WU-08(P2)、WU-10(P2) ｜ 人工学习校准（60f70e1 核心卖点）目前对操作者呈现"点了成功但没生效/看不见"。

## 目标

- 运营者在 WebUI 的每个校准动作真实生效且可见：编辑文本落库、权重所见即所得、pending_human 出现在待审队列、审批同步召回索引、驳回不回流、冷却面板有真数据、搜索跨页可用。

## 基线证据

- **WU-01**：`review_service.py:96-113` 的 `approved` 分支参数集不含 `replacement_expression/apply_replacement`（只有 `revised` 分支带，前端只发 approve/reject）→"编辑后保存并通过"的修订文本被静默丢弃。
- **WU-02**：`review_ui_service.py:223` `weight_delta=float(weight) - 1.0`——把 UI 的绝对权重当作"相对 1.0 的增量"；当前权重 2.0 时原样保存会变 3.0；降级路径 L260-264 却用 `weight-current`（正确），两路径不一致。
- **WU-03**：`expression_pattern_service.py:209-221` 人工待审队列只收 `{pending, revision_needed}`，排除 `pending_human`；徽标口径却含它——auto-check 升级人工的候选在队列里永远看不到（R09-04 修复的副作用，KNOWN_FIXED_REGRESSION）。
- **WU-05**：表达审批只改 store 状态不同步 `index_projector`（jargon L1185-1189 与 canonical L339-346 都同步，唯独 expression 缺失）→ 通过的表达在向量召回缺位直至重启。
- **WU-07**：黑话"驳回并删除"走 `hard_delete` 抹掉 rejected 墓碑，而 `jargon_miner.py:57-63` 的去重集合恰恰依赖含 rejected 的行 → 噪声词下轮挖掘回流待审，人工清理变打地鼠。
- **WU-08**：`admin_ui_service.py:1060`/`learningservice.py:43` 读不存在的 `_recent_patterns`（真名 `_recent_pattern_keys`，git log -S 证实从未存在）→ `/learning/cooldowns` 自首个提交起恒空。
- **WU-10**：`memory_ui_service.py:657-668` 关键字过滤发生在 LIMIT 之后且 total 未过滤 → 搜索对多页数据基本不可用。

## 实施步骤

1. WU-02（最小且独立）：`submit_review` 先 `get_pattern` 取当前权重再算 delta（与降级路径同法）。测试：current=2.0 提交 2.0，断言最终 2.0（现行代码得 3.0；注意现有测试 weight=1.2/current=1.2 恰好 delta=0 掩盖了问题，需换数值）。
2. WU-01：`review_ui_service.submit_review` 把 `replacement/apply_replacement` 并入主路径（extra_update 或 approved 分支接受 replacement）。测试断言 facade 成功路径下 `replacement_expression==编辑文本`。
3. WU-03：`review_service.list_pending_reviews` canonical 分支改独立查询（`review_status∈{pending,revision_needed,pending_human}`），**不再复用** `list_reviewable_patterns`（auto-check 继续用后者，其 L59 已显式跳过 pending_human，双保险）。
4. WU-05：审批状态变化后按 jargon 同款调用 `projector.project/cleanup_deleted`。
5. WU-07：`reject_jargon` 改为置 `status=rejected` 软墓碑（7 天 grace 交维护 purge——purge 调度在 OPT-05/WU-04 接通），同步修改"物理删除"文案。
6. WU-08：两处改读 `_recent_pattern_keys`（tuple key 序列化为字符串）。
7. WU-10：`list_jargon` 的 query 下推为 SQL LIKE/FTS（过滤后再分页、返回过滤后 total）；前端表达 tab 把 keyword 传 `/reviews?keyword=`（后端已实现）。

## 验收标准

- 上述 7 项各有单测锚定，全量 pytest 绿。
- WebUI 手测链路：编辑一条表达文本并通过 → `GET /memories/canonical/{id}` 内容为编辑后文本、权重等于输入值、`/memories/diagnostics/index` 无该 id 的 missing projection；构造 pending_human 候选 → 待审队列可见且 auto-check 不碰它；驳回一条黑话 → 对同群重跑挖掘不再回流；`/learning/cooldowns` 在 selector 有记录时非空；30+ 条黑话中搜索第 2 页词 → 第 1 页显示命中且 total=匹配数。

## 风险与回退

- WU-03 中风险：需确认 auto-check 不因此重新吃进 pending_human（已有 L59 跳过 + 新增成对断言）。
- WU-07 中风险：墓碑 purge 依赖 OPT-05 的维护调度接通后才会过期清理，接通前 rejected 行会累积（可接受，本来就该保留）。
- 其余低风险（提交链计算/属性名/查询下推）。各项独立提交可单独 revert。

## 完成记录

**2026-07-26 代码侧完成**（WebUI 手测验收待部署后执行）：

- 后端改动（5 文件）：
  - `review_ui_service.py`：WU-02 主路径先取当前权重再算 delta（与降级路径同法，测试证实 current=2.0 提交 2.0 → delta=0，旧代码 +1.0）；WU-05 新增 `_sync_expression_projection`（approved/replace → project，rejected → cleanup_deleted），facade 成功路径与降级路径都调用；projector getter 防御式获取。
  - `review_service.py`：WU-01 approved/rejected 分支携带 `replacement_expression + apply_replacement`（编辑通过/编辑驳回都保留人工文本）；WU-03 人工待审队列改独立查询（statuses=review_pending + review_status∈{pending,revision_needed,pending_human}），不再复用 auto-check 口径 `list_reviewable_patterns`（测试断言复用即失败）。
  - `memory_ui_service.py`：WU-07 `reject_jargon` runtime 态改软墓碑（update_jargon status=rejected → visibility=maintenance_only + 投影 cleanup），legacy-only 态保留物理删除（不参与挖掘去重）；WU-10 `list_jargon` 关键字改"先过滤后分页"（有界扫描 cap=2000，触顶显式返回 `search_scan_capped` 不做静默截断），total 为过滤后计数。
  - `admin_ui_service.py` + `learningservice.py`：WU-08 冷却端点改读真实属性 `_recent_pattern_keys` 并序列化（旧属性名自首个提交即不存在）。
- 前端改动（app.js 3 处）：表达全量 tab 关键字下推 `/reviews?keyword=`（后端两层路由已支持，核实 review_routes.py:21/plugin_pages.py:393）；黑话"驳回并删除/删除"按钮与确认文案对齐墓碑语义（"驳回/下架…同词不再回流待审"）。
- 语义决策记录：`jargon_all` 的"删除"与待审"驳回"共用同一 `/reject` 端点——两者统一为墓碑（下架已通过噪声词同样需要防回流）；噪声预检的批量物理删除走独立 cleanup 端点，保持不变。
- 测试：新增 `tests/regression/review/test_calibration_loop_hotfix.py` 12 条；**stash 红验证 10/12 变红**（2 负向对照保持绿）。既有测试两处更新：`test_runtime_jargon_delete_cleans_projection` 重写为墓碑契约（旧断言 physical_delete=True 锁定的正是缺陷）；legacy SQL 全链路测试因 runtime/legacy 分流**原封不动通过**。受影响区域 121 passed。
- 待部署手测：编辑通过后 `GET /memories/canonical/{id}` 内容为编辑文本、权重等于输入值；`/memories/diagnostics/index` 无缺投影；pending_human 出现在待审 tab；驳回黑话后同群重跑挖掘不回流；`/learning/cooldowns` 非空；跨页搜索命中且 total 正确。注意：墓碑 purge 依赖 OPT-05/WU-04 的维护调度接通。
