# AstrMai 优化计划索引

本目录是 2026-07-26 全量只读审计之后优化阶段的执行文档。基线证据见 `baseline-audit-20260727.md`；审计方法、交叉验证与去重记录见 `claude-audit-integration-20260727.md`；全量结构化数据在 `../../.agent/claude-full-audit-20260727/findings.json`（68 条独立修复单元，本目录 16 个 OPT 全覆盖）。每个 OPT 文档是一个独立工作流，包含目标、基线证据、实施步骤、验收标准和回退方式。后续开发按本目录推进，不靠记忆。

## 执行顺序与状态

| 编号 | 工作流 | 优先级 | 依赖 | 状态 |
|---|---|---|---|---|
| [OPT-01](OPT-01-group-reply-drop-hotfix.md) | 群聊在途回复丢弃止血（线程签名时序） | P0 | 无 | 代码完成（待线上 trace 复采验收） |
| [OPT-02](OPT-02-background-budget-leak.md) | 后台任务 contextvar 预算泄漏（记忆抽取复活） | P0 | 无 | 代码完成（待线上日志复采验收） |
| [OPT-03](OPT-03-proactive-chain-revival.md) | 主动行为链复活（wakeup/poke/签到） | P0 | 无 | 代码完成（待线上首条主动消息验收） |
| [OPT-04](OPT-04-review-calibration-loop.md) | 人工校准与审核闭环（WebUI 七连修） | P1 | 无 | 代码完成（待 WebUI 手测验收） |
| [OPT-05](OPT-05-memory-data-quality.md) | 记忆数据质量与维护调度（偏好覆盖/幽灵向量） | P1 | 无 | 代码完成（purge 默认关，观察后开启） |
| [OPT-06](OPT-06-memory-read-path.md) | 记忆读取链恢复（注入率 2.9%→目标区间） | P1 | OPT-02 | 代码完成（待注入率/时延复采） |
| [OPT-07](OPT-07-turn-budget-unification.md) | 延迟预算统一（tool/vision 纳入 turn 预算） | P1 | OPT-02（已完成） | 代码完成（待事故样本复采归零） |
| [OPT-08](OPT-08-llm-cost-reduction.md) | 模型调用成本削减（mood/cognitive/judge 缓存） | P1 | 量化验收建议先做 OPT-11 口径 | 代码完成（待调用量复采 + judge A/B） |
| [OPT-09](OPT-09-provider-pool-robustness.md) | Provider 与模型池健壮性（not-found/级联副作用） | P1 | 无 | 代码完成 |
| [OPT-10](OPT-10-config-single-source.md) | 配置真源与容错（死键清理/降级加载） | P1 | 无 | 代码完成 |
| [OPT-11](OPT-11-observability-contract.md) | 观测契约完整性（funnel/口径/trace 存储） | P2 | 无 | 代码完成（结构迁移另立专项） |
| [OPT-12](OPT-12-tool-chain-fixes.md) | 工具链路修复（图片轮工具可见性等） | P2 | 无 | 代码完成 |
| [OPT-13](OPT-13-regression-safety-net.md) | 回归测试安全网（身份/预算/契约五张网） | P1 | 与对应修复同批 | 核心完成（TG-07 专项遗留） |
| [OPT-14](OPT-14-lifecycle-resilience.md) | 生命周期与重载韧性（重载失忆/闩锁） | P2 | 无 | 未开始（需运行环境取证） |
| [OPT-15](OPT-15-learning-dream-governance.md) | 学习与 Dream 治理（毒丸/幻觉晋升） | P2 | 无 | 代码完成 |
| [OPT-16](OPT-16-interaction-polish.md) | 交互打磨（私聊话术/撤回/空态三义性） | P2 | 无 | 部分完成（ID-08 留专项） |

推荐主线：**OPT-01 → OPT-02 → OPT-03 → OPT-04 → OPT-06 → OPT-07 → OPT-08**（先止住用户正在受害的三个 P0，再修运营者手里的校准工具，然后恢复记忆读取、统一预算、削成本），OPT-13 的测试按"先补测试再改代码"纪律拆进对应 OPT 同批落地，其余按依赖穿插。

## 执行纪律

- 每个 OPT 开工前先读对应文档与 `findings.json` 中对应条目（每条含证据行号、最小修复边界、回归风险）；实施中发现与文档冲突，先改文档再改代码。
- **先补测试再改代码**：改动区域缺特征测试的，第一步把现状行为固化成测试（OPT-01/02/07 涉及的 gate/planner/coordinator/budget 是最近 5 个提交的热改区，尤其如此）。
- 每完成一个 OPT：跑 `python -m pytest -q -k "not test_project_files_do_not_embed_local_absolute_paths"` 全绿，再执行该 OPT 的专属验收命令，然后把上表状态改为"已完成"，并在 OPT 文档末尾"完成记录"填实测前后对比。
- **行为类修复必须用线上 trace 复采验收**：部署后拉新一轮 `turn_trace_samples` 快照（对齐 `.agent/runtime-observability-*` 的采集方式），跑 `PYTHONIOENCODING=utf-8 python scripts/analyze_turn_ledger.py` 与基线对比（基线数字在 `baseline-audit-20260727.md`）。
- **完成一块提交一块**：每个 OPT（或其子项）独立提交，保证任何一步可单独 revert；`git add/commit/push` 需用户明确授权。
- veracity=LIKELY / NEEDS_RUNTIME_EVIDENCE 的条目（TL-04、TL-06、RT-11、ML-08、ID-07、PL-10）动手前先按文档里的取证方法拿到证据，否则降级为观察项，不做防御性大改。
- 服务器 DB（`/AstrBot/data/plugin_data/astrmai/astrmai.db`）相关验证 SQL 已写入各 OPT；只读采样，不在生产库上做写操作演练。
- Windows 控制台是 GBK：所有分析脚本带 `PYTHONIOENCODING=utf-8`，或输出写文件再看。
- 不顺手修复范围外问题；历史审计中判定为"设计行为/不可达"的项（见 `.agent/final-76-bug-reaudit.md`）不要翻案重修。
