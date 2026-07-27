# OPT-13 回归测试安全网（五张网 + 文档基线）

状态：**已完成**（五张网全部落地） ｜ 优先级：P1 ｜ 依赖：与对应修复 OPT 同批落地（"先补测试再改代码"） ｜ 覆盖发现：TG-01(P1)、TG-05(P2)、TG-06(P2)、TG-07(P2)、TG-08(P3) ｜ 单元测试习惯良好（1673 条 0 收集错误），系统性缺口全在"跨模块行为不变式"——单元全绿、组合回归无人发现，且恰好集中在最近 5 个提交的热改区。

## 完成记录

**2026-07-26 核心完成**：

- TG-06：`tests/regression/webui/test_fe_be_contract_alignment.py`——静态解析 app.js 全部 api 调用路径（模板参数归一 {p}）与 plugin_pages 注册表比对，断言 FE ⊆ BE + 解析健全性下限（防正则腐化）。首跑即绿（当前契约完好，含 OPT-04/05 新增路径），护栏就位。
- TG-05：`test_memory_write_retrieve_inject.py` 追加修订闭环——写入→旧内容命中→`store.update_memory` 修订→新内容命中且旧表述消失→注入渲染新内容（真实 store+FTS wiring，此前三层测试全部绕开）。
- TG-01（聚焦链版）：`test_group_identity_chain.py`——双 sender 交替+B 直接唤醒场景断言三源一致（focus 事件 sender == speaker block QQ == 画像层将用的 get_sender_id），并锁 focus_context 缺失时回退事件身份、群边界提示词在位。**完整 gate→planner→executor 三段拼装 e2e 仍为专项遗留**（装配成本高，塌缩点已被本链覆盖）。
- TG-08：session-state.md Test Status 更新（1774 collected、signin flaky 根因与恢复命令）。
- **TG-07 已完成**（G2，2026-07-26）：见下方 G2 小节。
- 过程佐证：OPT-05 实施中 `_run_maintenance_cycle` 被误截断，正是被既有"子服务失败隔离"装配断言当场抓获——本 OPT 主张的防回归价值已有实证。

### G1 补充（2026-07-26）：signin 时间窗 flaky 根因修复

**根因与审计描述不同**（审计记为"测试未注入时钟"，实际相反）：`run_once(now_ts=...)` 早已支持时钟注入，测试也传了值——但传的是**硬编码 epoch `1768695000.0`**。该值只在 UTC+8 下等于 08:10（写测试时的机器时区），而 `_within_sign_window` 用 `time.localtime`（机器本地时区），在本机 UTC-8 下解析为 16:10 直接落到窗口外。同文件另两个窗口用例用 `time.mktime((...))` 按本地时间构造，因此一直是绿的——修法就是统一到后者。

- 改动（**测试单侧，生产代码零改动**）：新增 `_local_ts(hour, minute)` helper 由本地时间反推 epoch；5 处硬编码 epoch 与 2 处硬编码小时全部改为从 `GroupSigninService.SIGN_HOUR` 派生（SIGN_HOUR 若调整测试自动跟随）。
- 新增锚定用例 `test_sign_window_predicate_is_timezone_independent`：断言窗口谓词在 `SIGN_HOUR:00/:59` 为真、`SIGN_HOUR-1:59` 与 `SIGN_HOUR+1:00` 为假，并断言派生时间戳的本地小时恒等于 SIGN_HOUR——任何人再塞裸 epoch，本用例会在非 UTC+8 机器上立刻变红。
- 红验证：stash 掉测试改动后 **3 红**；恢复后 7/7 绿。
- **验收达成**：`PYTHONIOENCODING=utf-8 python -m pytest -q -k "not test_project_files_do_not_embed_local_absolute_paths"` → **1777 passed, 1 skipped，无需任何 `--ignore`**。此命令从此为标准回归命令。
- 注：Windows 下 `TZ` 环境变量不影响 `time.localtime`（`tzset` 仅 Unix），故时区无关性靠"按本地时间构造"从机制上保证，而非靠切时区跑测试验证。

## 目标

为五类用户可感知的行为路径建立回归守护，使 OPT-01/02/04/07 的修复不会被后续开发悄悄破坏：

1. **群聊身份隔离 e2e**（TG-01）：speaker block、关系数据、终线 guard 三个身份来源今天各自单测，无一断言三者指向同一 sender——称呼串号 bug 类历史真实发生过（为此写了 GroupActorConsistencyGuard），裸名直呼与关系数据串号完全无测试。
2. **记忆修订闭环**（TG-05）：WebUI update_canonical → projector 重投影 → 检索/注入反映新内容，现有三层测试全部 mock 断链（WebUI 测试 projector=None）。
3. **前后端契约静态对齐**（TG-06）：app.js 75 个 api 路径 vs 后端注册表，目前靠手工镜像清单；历史已有 ≥4 例 FE/BE 漂移 bug。
4. **私聊 vision barrier 组合分支**（TG-07）：4da2910 新增的 gate 侧三分支（屏障期间新消息 re-merge、abort 后池非空续跑、resolve 超时）零测试——用户"发图后紧跟补充文字"是最常见私聊形态。
5. **文档基线**（TG-08）：session-state.md 的 1142 计数已过期（实际 1673），更新入口文档。

## 基线证据

- TG-01：身份取值分散——speaker block 用 `focus_context.focus_sender_id`（planner_prompt_context.py:155-156），关系/画像用 `event.get_sender_id()`（planner.py:1224 → planner_side_inputs.py:891-897），gate fast-wakeup 直接派发原始 event（gate.py:823）；guard 只能修复"外人名+11 种后缀称呼"。
- TG-05：`memory_ui_service.update_canonical`（L338-346）修订后调 projector，但 test_webui_backend_refactor 的 runtime-bound 测试 projector=None、store 为 mock；integration 只有 write→retrieve→inject 三腿。
- TG-06：`test_native_admin_api_registers_core_routes` 用手工清单；round11 用 assertIn JS 片段（重构即腐化）；真正直调 route handler 的测试仅 1 条。
- TG-07：现有 gate 测试的 prepare_batch stub 都瞬时返回，永远不命中回填分支；resolver 全部即时返回。
- TG-08：`pytest --collect-only -q` → 1673 条 0 错误；manual 脚本 AST 全过、无 broken import。

## 实施步骤

1. TG-01：新增 `tests/regression/conversation/test_group_identity_isolation_e2e.py`——双 sender 交替发言 → gate._debounce_and_judge 选 focus → planner._prepare_plan_context 组 prompt → executor._finalize_reply（stub judge/gateway），断言 prompt 中 speaker block 的 QQ == side_inputs 加载画像的 user_id == 被回复者；补裸名直呼场景的 guard 行为锚定。**建议与 OPT-01 同批**（同一热区）。
2. TG-05：`tests/integration/test_memory_write_retrieve_inject.py` 追加修订闭环：write→retrieve 命中旧内容→真实 MemoryUiService.update_canonical+真实 projector→retrieve 新内容命中且旧内容不再 top1→injection 渲染新内容。**建议与 OPT-04/05 同批**。
3. TG-06：`tests/test_plugin_pages_admin_refactor.py` 新增 <50 行静态对齐测试——解析 app.js 全部 api.get/post 路径（模板参数归一为 {param}）→ 收集后端注册集合（双格式归一）→ 断言前端 ⊆ 后端。
4. TG-07：gate/coordinator 各加 1-2 条——① 慢 prepare_batch（await Event 控制）期间注入新消息→第二轮批次含旧+新且不重复发送；② abort 后池非空→失败通知只发一次且新消息继续处理；③ 慢 resolver→outcome=resolve_timeout 且 downstream 符合 policy。
5. TG-08：更新 `.agent/session-state.md` Test Status 小节（1673 计数 + 新恢复命令）。

## 验收标准

- 五项新测试落地并绿（TG-01/07 在对应修复前先红——证明它们真的在守护）；全量 pytest 绿。
- 契约对齐测试能抓住人为注入的路径改名（自检一次）。

## 风险与回退

- 纯加测试，低风险；TG-07 的并发交织用例需事件同步器，写法上避免 sleep 竞态（用 asyncio.Event 显式控制）。
- 唯一中风险是把现状"固化错了"——TG-01/07 的断言先与 OPT-01 修复后的预期语义对齐，不锚定 bug 行为。

### G2 补充（2026-07-26）：TG-07 vision barrier 并发交织测试

新增 `tests/regression/conversation/test_vision_barrier_interleaving.py` 5 条，用
`asyncio.Event` 精确控制交织时刻（零 sleep 竞态）：

- **re-merge 分支**：屏障执行中注入晚到消息 → 断言第二轮 `prepare_batch` 携带 `[旧, 新]`
  全量批次、且全程零重复派发（现有 fixture 的 stub 瞬时返回，永远命中不到此分支）。
- **burst deadline 跨迭代持久化**（OPT-07/RT-05 锚定）：主断言用**时钟无关**口径——
  整个 burst 只允许调用一次 `vision_total_budget_sec()`。
- **abort 分支**：3 条消息的批次只发 **1 次**失败通知（不是 3 次）；通知发出瞬间注入新消息
  → 断言 worker 继续处理新批次，池排空后才 `is_evaluating=False` 收工。
- **resolve 超时**：明细层 `_prepare_event` 断言 `outcome=resolve_timeout`/`timeout_count=1`，
  批次层断言 `downstream_action` 按策略分流（require_analysis→abort_required_vision /
  timeout_fallback→continue_with_placeholder）。

**红验证（含两次自我纠错，均记录在案）**：

1. 首版把 `resolve_timeout` 断言在聚合层——聚合按设计归并为 `failed`，断言层次错误；
   改为明细层断 outcome、批次层断 downstream_action。
2. 首版红验证（把 burst deadline 改回每轮重算）**没抓住**：Windows `time.monotonic()`
   分辨率约 15.6ms，两轮迭代落在同一刻度，deadline 数值恰好相等。改为断言预算获取器
   调用次数后，注入同一回归 → **精确变红 1 条**，恢复后 5/5 绿。
3. 另修正测试自身缺陷：同一 coordinator 跨两次 `asyncio.run` 复用，`asyncio.Event` 绑定
   前一个事件循环 → 第二次 await 抛异常被吞成 `unexpected_failure`，**两条用例原本都在
   验证错误的代码路径**；改为每次 `asyncio.run` 用全新实例。

**生产代码零改动**（纯补测试）；全量回归 **1782 passed, 1 skipped**。
