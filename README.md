# AstrMai

AstrMai 是面向 AstrBot 的事件驱动型角色聊天插件，提供对话注意力、人格压缩、长期记忆、图片理解、主动互动、QQ 工具调用和管理页面。

## 运行要求

- AstrBot `>= 4.26.4, < 5`
- 已验证 AstrBot `4.26.4` 与 `4.26.6`
- Python 3.12
- 可用的文本模型；图片理解需要额外配置视觉模型
- FAISS、TTS、Sys3、Computer、Cron 均为可选能力，缺失时插件会按功能降级

依赖列在 `requirements.txt`。AstrBot 安装插件时可自动安装，也可以在 AstrBot 的 Python 环境中执行：

```bash
pip install -r requirements.txt
```

## 安装

1. 将发布包目录命名为 `astrmai`。
2. 放入 AstrBot 的 `data/plugins/` 目录。
3. 在 AstrBot WebUI 中启用插件。
4. 等待日志出现：

```text
[AstrMai] boot complete — runtime running
```

不要把源码仓库整体复制到生产环境。正式发布包不应包含 `tests/`、`.agent/`、`.claude/`、虚拟环境、数据库、日志或本机缓存。

## 首次配置

至少需要确认：

- **模型池**：Judge、对话、记忆、视觉等任务使用的模型。
- **接管范围**：私聊是否启用、群聊范围和注意力策略。
- **人格**：AstrBot 当前人格已可读取；启动时会先生成并持久化核心人格。
- **时间设置**：模型请求、整轮预算、视觉等待和回复过期时间。
- **记忆**：召回数量、混合检索、向量模型和维护策略。

配置项均在 AstrBot 插件配置页显示中文名称和说明。旧配置中的越界值会被记录并回退到安全默认值，不应导致整个插件拒绝加载。

## 数据目录

运行数据由 AstrBot 写入插件数据目录，典型容器路径为：

```text
/AstrBot/data/plugin_data/astrmai/
```

其中可能包含：

- `astrmai.db`：会话、记忆、画像、学习和视觉记录。
- `persona_cache.json`：人格核心、说话方式、第一人称改写和八维切片。
- `cache/`：上下文快照和可观测账本。
- 表情包及其他运行资源。

发布包不携带这些文件。新安装时数据目录应由 AstrBot 和插件自行创建。

## 管理页面

插件提供 AstrBot 原生 Plugin Page：

- 页面目录：`pages/admin/`
- 入口：AstrBot WebUI -> 插件 -> `astrmai` -> 插件页面 -> `admin`

管理页用于查看运行状态、主动学习、表达与黑话审核、记忆网络、用户画像和角色切片。页面必须从已登录的 AstrBot WebUI 打开，不能直接双击本地 HTML。

## 可选能力与降级

- **视觉**：可选择“等待图片识别完成”或“超时后以 `[图片]` 继续”。视觉模型不可用时不应阻塞纯文本聊天。
- **向量检索**：embedding 或 FAISS 不可用时，记忆召回降级到 canonical FTS/BM25 与 fallback。
- **TTS**：通过独立 TTS 插件增强；失败时保留文本回复，不向用户发送内部错误。
- **Sys3 / Computer / Cron**：未启用或宿主能力缺失时保持聊天模式。
- **QQ 工具**：由 NapCat/AstrBot 提供实际能力；查询或发送失败时返回真实原因，不凭空构造成功结果。

## 升级

升级前同时备份：

1. 当前插件目录。
2. `data/plugin_data/astrmai/` 数据目录。
3. 当前插件配置。

然后用新的干净发布包替换插件业务文件。不要覆盖数据目录，也不要把发布包内不存在的数据目录解释为需要删除生产数据。

升级后检查：

```text
Plugin astrmai (...)
[AstrMai] memory skeleton initialized
[AstrMai] persona core ready and persisted
[AstrMai] boot complete — runtime running
```

再检查最近日志中没有 AstrMai 相关的 `Traceback`、`ValidationError`、`ModuleNotFoundError` 或 `AttributeError`。

## 回滚

1. 在 AstrBot WebUI 中禁用 AstrMai。
2. 恢复升级前备份的插件目录。
3. 仅在发生数据结构不兼容且确认需要时恢复数据目录备份。
4. 重新启用插件并检查启动日志。

代码回滚与数据回滚应分开处理，避免用旧数据库覆盖升级后产生的新聊天记录。

## 构建发布候选

在源码仓库中执行：

```bash
python scripts/build_release_candidate.py C:\tmp\astrmai-release-candidate
```

输出目录必须位于源码仓库之外。构建器使用白名单复制运行文件，并拒绝数据库、日志、缓存、测试和虚拟环境进入候选包。

## 验证

开发环境建议执行：

```bash
python -m pytest -q -k "not test_project_files_do_not_embed_local_absolute_paths"
python -m compileall -q main.py config.py astrmai pages scripts
git diff --check
```

真实环境验收还应覆盖私聊、群聊、图片、记忆召回、工具调用、插件禁用后重新启用和管理页。

## 故障排查

- **插件无法加载**：先查看完整 `ValidationError` 或导入堆栈，不要只看最后一行。
- **回复很慢**：核对“时间设置”中的整轮预算、模型请求超时、视觉策略和回复过期时间。
- **图片未理解**：确认视觉模型池可用，并检查视觉策略是否允许超时降级。
- **记忆无结果**：检查写入记录、索引投影状态、embedding/FAISS 可用性和召回 trace。
- **管理页空白或 500**：确认插件运行时已完成启动，并从 AstrBot Plugin Page 入口打开。
- **其他插件命令无响应**：AstrMai 对其他插件命令应只跳过自身处理，不应调用 `event.stop_event()`。
