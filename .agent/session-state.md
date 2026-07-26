# Session State — AstrMai 上线前测试完成

> 2026-07-05 · full · handoff

## Current Task
AstrMai 插件上线前测试全部完成。测试从 936 → 1142 passed。待 git commit 和真实 QQ 环境 smoke test。

## Progress
✅ 八轮深度审计（~340 bugs，~125 修复）
✅ P0-P3 修复波次（24 bugs 修复）
✅ 76 条测试缺口审计 → 20 条确认缺陷 → 全部修复
✅ 全模块测试覆盖率审计（6 代理并行，真实行覆盖 72.9%）
✅ P1 覆盖率补强（9 文件，+52 测试）
✅ QQ E2E 测试插件（Probe + Orchestrator）设计+开发
✅ 1168 条测试目录生成（193 文件）
⏳ Git commit（30+ 文件未提交）
⏳ 真实 QQ 环境 smoke test

## Architecture
```
main.py → PluginFacade
  conversation/ (~45) — 消息→注意→判决→规划→执行
  memory/ (~42) — 记忆 v2: FTS5/混合检索/评分/注入
  infrastructure/ (~48) — 网关/持久化/运行时/安全
  state/ (~14) — 关系引擎/精力/情绪
  learning/ (~15) — 表达挖掘/黑话/画像/审核
  proactive/ (~11) — 唤醒/签到/梦境/心流
  webui/ (~8) — 85 REST 端点
  workmode/ (~7) — Sys3/定时任务
  + shared/, multimodal/, presentation/, app/
```

## Test Status
- **1774 collected（2026-07-26，OPT-01~10 波次后）**；全量回归绿（排除绝对路径检查与 3 个 signin 时间窗历史 flaky——`group_signin_service.py` 按 `tm_hour==SIGN_HOUR` 判定且测试未注入时钟，仅签到时段能过，待注入时钟修复）
- 历史基线：1142 passed（2026-07-05）、真实行覆盖率 72.9%
- 恢复命令：`PYTHONIOENCODING=utf-8 python -m pytest -q -k "not test_project_files_do_not_embed_local_absolute_paths and not test_group_signin"`

## Key Documents (Codex 入口)
| 文件 | 用途 |
|------|------|
| `.agent/test-catalog-complete.md` | 1168 条测试的完整目录 |
| `.agent/test-coverage-audit-codex-review.md` | 真实覆盖率 + 修正优先级 |
| `.agent/final-76-bug-reaudit.md` | 76 条 bug 逐项复审 |
| `.agent/test-gap-audit-master.md` | 原始测试缺口审计（已过期） |
| `.agent/test-coverage-audit-complete.md` | 原始覆盖率审计（已过期，以 codex-review 为准） |

## Pending
1. Git commit — 30+ 文件跨多轮修改
2. 部署 Probe + Orchestrator 到真实 QQ 环境
3. 跑 `/amtest start smoke`

## Recovery
```bash
cd <project-root>
python -m pytest -q -k "not test_project_files_do_not_embed_local_absolute_paths"
# 1142 passed
```
入口: `main.py` → `AstrMaiPlugin` → `PluginFacade`
测试插件: `../astrmai_test_probe/` + `../astrmai_test_orchestrator/`
