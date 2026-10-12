# Prompt Context 本地验收记录

日期：2026-10-11。基线：`6d945d5aeec2eab0a84e9cf88462bc83aeeb74e5`。

本次完成身份、历史连续性、昵称/引用唤醒的本地实现与回归收尾，状态为 `ready_for_independent_revalidation`。用户已选择先完成本地、宿主验收待办；本机 Python 3.11.4 / pytest 8.4.1 / AstrBot 4.14.6 不等同于目标 Python 3.12 / AstrBot 4.26.4。没有构建候选、同步生产、提交、推送或部署。

## 本地结果

- 最终全仓运行：`4056 passed, 1 skipped, 426 warnings`，退出码 0；运行范围为 `tests --ignore=tests/manual`，没有用文件顺序、skip/xfail 或排除失败用例规避问题。
- 修改后身份/历史/唤醒/AtWake 重点回归：349 passed；AtWake 与模块隔离正反顺序同一进程各 116 passed。
- 成功全仓运行语句覆盖率 81.35%、含分支 78.08%；专题 9 模块含分支 80.49%，Reply 修复函数语句/分支均 100%。更严格的全仓含分支 >80% 仍未达到，不能混用口径。
- 四项性能、编译、隔离 cwd 的 main 导入、Ruff 基础 E9/F63/F7/F82 和专题 9 文件 mypy 均通过；这些检查不代表全仓严格类型或所有 lint 规则。完整命令详见 `plan/prompt_context/06_audit.md`。
- 原始技能检查全目录仍有 5 error：4 条来自旧 `.agent/cloud_exports` 的 BOM，1 条来自故意验证 requests 不得绕过网络隔离的测试。运行源码按原检查函数扫描为 0 error / 43 warning。不能称原始全目录扫描通过，也没有修改检查器、历史导出或安全测试来隐藏错误。

## 改动依据

1. 空正文引用只保留引用语义，当前消息的有效 Plain 或新增媒体才能作为 Reply 追问；结构化 @Bot 继续独立生效。
2. `test_embedding_refactor.py` 的收集期全局 AstrBot 替身安装导致后续 main 导入缺少 filter；移除无必要安装，新增模块对象、模块属性和缺失状态的恢复 helper，覆盖异常退出。
3. executor/外部结果测试逐用例声明替身依赖，不再依赖前面的测试泄漏；日记任务测试控制已有随机延迟，不改业务延迟。
4. 五个话题旧断言按当前合同保留“历史可读、过期 bridge 拒绝”，没有放宽拒绝、TTL 或发送断言；其他旧测试按实际版本号、stage ledger、身份格式及有限历史读取合同修正。
5. 基础静态检查发现的缺失 import、导出名称和类型推断做局部修正；BOM 只移除文件头，计划绝对路径只替换为工作区占位符。

## 开发数据隔离事故

本轮早期全仓测试通过缓存的真实 AstrBot 数据路径函数写入了开发目录既有 `data/plugin_data/astrmai/memory/memory_v2.db`。该库创建于 2026-09-20；发现时 16 KB、user_version=0、vector_resource_descriptors 5 行。发现后 SHA-256 为 `392dd9210bb67669c174a883801f9004790efcfc947553768bcc2b96432d541d`，后续复核一致。没有开工前内容备份或哈希，不能确认原内容未变；没有擅自删除、覆盖或回滚该库。

此后添加 session 级 `ASTRBOT_ROOT` 隔离及真实路径函数断言，后续运行仅使用独立临时根。生产目录及生产数据库未修改；不能据此宣称“本轮所有数据库均未修改”。

## 交接边界

证据位于系统临时目录下的 `astrmai_prompt_context_acceptance_20261011_4e791a/`，完整命令、失败迭代、最终覆盖率口径和状态见 `plan/prompt_context/06_audit.md` 与 `07_handoff.md`。`plan/` 仍受现有 Git 忽略规则保护；本地审计不等于已经提交到远程。

独立复验和真实宿主/QQ 投递、长期行为待办。候选包不含本轮增量，不能直接发布；15 秒取消来源仍是“外部取消候选，来源未证实”。
