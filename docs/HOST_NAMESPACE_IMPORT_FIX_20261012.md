# 宿主命名空间导入故障与最小修复

状态：`ready_for_host_validation`。上一轮发布已部署后回滚，`release_accepted_for_production: false`；新包不能沿用旧探针的宿主通过结论。

## 故障证据及修复

操作员报告目标 Python 3.12.13 / AstrBot 4.26.4 上逐文件校验、编译和顶层模块探针通过，但进入 planning 的轮次全部错误。约四分钟后回滚到 10-06 构建，回复恢复；配置逐字节恢复，数据库 user_version 保持 175。服务器证据来自操作员，本轮没有访问服务器或另行验证这些数值。

`planner_prompt_context.py` 的 `_build_current_speaker_block` 在非空 turn_target 分支使用绝对 `astrmai` 导入。宿主按 `data.plugins.astrmai.astrmai.*` 装载，顶层 `astrmai` 不存在，实际调用时抛 `ModuleNotFoundError`。此前本地测试及服务器 probe 将插件根放入 sys.path，又只导入模块，遗漏了宿主装载形状和函数内导入执行。这是本次交付的验证缺口。

修复仅把 TargetKind 导入移到模块顶部，使用 `from ..contracts.turn_target import TargetKind`。不改变身份判断、唤醒、超时、发送、配置或数据库合同。main.py 六个、config.py 一个现有装载期 fallback 保留；开发树离线 replay 顶层导入不在运行白名单内，不顺手改动。

## Red / Green 与回归

`test_candidate_speaker_prompt_uses_host_namespace` 在隔离子进程使用 `-I -B`，构建宿主目录形状，用真实 package spec 装载插件及 main、contracts、planner。只提供 namespace 父级，不装替身、不设置顶层别名；使用真实 FocusThreadContext / ActorSet / TurnTarget 与合成事件外壳，实际调用引用身份分支，检查 A 发言、引用 B、@C 语义及顶层包不可见。

`test_release_validator_rejects_absolute_internal_imports` 证明函数内 from/import 及裸 import astrmai 均被 AST 检查拒绝，相对导入允许。构建器对发布 astrmai 包内所有作用域执行禁令；根 main/config fallback 保留。

- Red：2 failed，rc=1；实际函数精确在旧第 261 行抛 `No module named 'astrmai'`，另一项证明构建器未拦截。
- 首次 Green：业务调用成功，报告 JSON 被 AstrBot stdout 日志干扰；1 failed / 1 passed，rc=1。改为固定前缀报告行，不改业务断言。
- 最终 Green：2 passed / 1 warning，rc=0。
- 发布合同、身份、最终 Prompt、conversation、architecture 与相邻 ported 回归：376 passed / 10 warnings，rc=0。
- 运行源码与两个脚本 compileall（排除既有 venv，缓存隔离）：rc=0。
- 全仓技能扫描：5 error / 1050 warning，rc=1；错误集合与上一轮 5 error / 1051 warning 相同，四个 .agent 历史导出 BOM、一个测试故意验证 requests。不能称全仓通过。

完整命令、日志与候选证据位于 `{系统临时目录}/astrmai_host_import_fix_20261011_4b39679ec0/`。red.log、green.log、green2.log、regression.log、source-compile.log、skill-source.log 保留失败和成功；目录名创建于跨日边界，交接日期为 10-12。候选哈希、逐文件对照、命令/退出码和最终包状态见外置 PACKAGE_REPORT.md、commands.json、preflight.json、final-verification.json。

## 服务器复验要求

单独携带 `scripts/probe_host_import_shape.py`，在目标 3.12.13 / 4.26.4 的 staging 副本上执行：

```bash
python -I -B /path/to/probe_host_import_shape.py /path/to/staging/plugin
```

要求 rc=0、报告 passed=true、module 为 data.plugins.astrmai.astrmai.conversation.planning.planner_prompt_context、function_called 正确、top_level_astrmai_available=false，且 prompt_module_sha256 与新 manifest 一致。不要加入插件根 sys.path 或顶层别名。之后仍须真实入站到 planning、发送回执与 QQ 投递回放；探针不初始化插件，不证明全链路或长期行为。

本机 Python 3.11.4 / AstrBot 4.14.6 的成功不等于目标宿主验证。视觉 HTTP 400 / pool exhausted 是操作员确认的既有独立问题，本轮不处理；损坏服务器 staging 由操作员另行决定。

按用户已有授权更新指定本地包，不备份、不制作未经约定的压缩归档。不提交、推送、访问服务器、部署、修改配置实值或数据库。开发库 hash 须保持 `392dd9210bb67669c174a883801f9004790efcfc947553768bcc2b96432d541d`；早期测试数据事故保留原记录。
