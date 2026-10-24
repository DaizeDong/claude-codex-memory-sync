# Claude → Codex 配置桥接

将同机 Claude Code 的技能、指令、兼容 MCP 配置、已审阅的 hook 适配器及项目记忆接入 Codex。先预览并备份受管改动，再应用；所有权校验保护后续人工修改，原生记忆投递只覆盖明确授权的范围。

[![许可证：MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![语言](https://img.shields.io/badge/Languages-EN%20%2F%20CN-blue?style=flat)](#语言)

[English](README.md) | [中文版](README_CN.md)

> 这是独立的社区项目，与 Anthropic 或 OpenAI 无隶属或官方背书关系。

## 受管配置同步

使用 `sync-all.cmd` 同步支持的配置组件。要求 `PATH` 中有 **Python 3.11 或更新版本**；Windows 目录链接使用 junction。

先按锁定的公开版本安装源码和依赖：

```powershell
python -m pip install -e "$(git config --get fleet.guards)"
python -m pip install -r requirements-dev.txt
```

文件系统提供 Windows 8.3 短路径时，可以用它指定目录。Hook 计划保留调用方传入的目标路径写法，
生成的命令使用解析后的路径；目标目录中的 junction 和符号链接仍会在写入前被拒绝。

```powershell
# 零写入预览；未指定模式时也是这个行为。
.\sync-all.cmd --dry-run

# 查看冲突、跳过来源和兼容性详情，不输出来源正文。
.\sync-all.cmd --dry-run --json

# 先建一份回滚用的临时副本再应用当前计划，运行结束即删除。
.\sync-all.cmd --apply --json
```

支持的同步范围：

| Claude 来源 | Codex 结果 |
|---|---|
| 用户 skills、已启用且已安装插件的 skills | 在 `~/.agents/skills` 建立目录链接；保留已有无关技能，报告同名冲突。 |
| 用户和插件的 commands、agent 角色 Markdown | 转换为 skill 指令适配器；支持的 agent 定义同时生成 Codex 原生角色，无法表达的执行或工具权限限制单独报告。 |
| 全局 `CLAUDE.md` 和用户 rules | 合并到 `~/.codex/AGENTS.md` 的哈希受管区块，保留既有正文；受管区被手动修改时报告冲突，源规则的路径过滤仍保持其作用域。 |
| 项目 `CLAUDE.md` | 增加兼容的项目文档回退设置，保留 Codex 原有项目指令。 |
| 兼容的用户和已启用插件 MCP 定义 | 增量合并 `config.toml` 受管区块；现有原生 MCP 名称优先，缺失环境变量和不支持的传输或设置会列入报告。别的写入方塞进受管区块里的整张表（Codex 桌面版会把新表追加在它认识的最后一张表后面）会被挪到区块之后，前提是区块其余部分仍与记录的哈希一致、解析后的配置完全不变；挪动记在 `config.foreign_tables`，apply 之后也记在运行结果的 `foreign_tables_relocated`。区块内其他改动仍判冲突。 |
| 所有非空项目记忆目录 | 归档到 `~/.codex/imports/claude-memory/`，生成项目索引；经明确授权后，由持久 outbox 向 `ad_hoc` 投递小型增量说明。 |
| Claude hooks | 为 SessionStart、PostToolUse 和 Stop 提供已审阅的适配器，覆盖浏览器隔离检查和文档预算反馈；保留现有 Codex hooks，明确报告不支持的源命令。启动命令用正在运行的环境的基础解释器跑桥接脚本，从不写虚拟环境自己的 `python.exe`，用户家目录下的路径经 `$env:USERPROFILE`（`command` 里是 `$HOME`）表达。运行时可能需要原生信任及新会话。 |

这套实现用于**同一台机器**：链接的 skills 和适配后的本地脚本仍依赖原安装目录，不是换机可独立使用的完整复制。同步保留 Codex 当前模型、provider、认证和权限配置，不导入 Claude 登录及会话状态。

默认来源为 `CLAUDE_CONFIG_DIR`，未设置时用用户主目录下的 `.claude` 目录；默认目标为 `CODEX_HOME`，未设置时用 `~/.codex`。需要时可传 `--claude-home`、`--codex-home`、`--skills-home`。当两份配置并存时，优先使用活体 `.claude/.claude.json`，再回退到旧版 `~/.claude.json`。

### 记忆归档行为

完整同步递归读取 archive 目录以外的 `*.md`，排除空项目目录；项目路径来自当前 Claude 配置的精确编码匹配，也可通过已审阅的路径证据补充。现有映射和冲突优先保留，无法映射的项目保留原 key，不猜测路径。支持 UTF-8/BOM 和带 BOM 的 UTF-16，统一输出 UTF-8；单文件上限 1 MiB，拒绝 symlink/junction，跳过疑似凭据。报告仅显示跳过文件名和原因，不显示正文或凭据值。

控制字符**只在归档副本中**转成 `\u0008` 等可见转义，报告记录字符编码和位置，源文件保持不变。明确的 API key 文档占位模板可以保留，真实凭据匹配仍会拦截。

每次运行都重新计算内容哈希。新安装和默认调用只维护归档；存在 `instructions.md` 仅表示投递入口可用，不代表用户已经授权。使用 `--request-id` 和 `--scope` 明确选择投递请求，`--periodic` 保存已批准的周期授权。现有周期授权可通过文档中的授权 API 迁移，无需修改调度任务。

投递通过持久 outbox 记录增量及其前序，重复出现的 A 到 B 变化也各有记录。中断后无法证明是否成功的投递标为 `delivery_unknown`，不会自动重发；note 通过操作系统的禁止替换发布方式创建，已存在的 note 和用户修改都会保留。outbox 只留投递要用的东西（每个记忆文件的最新一条事件，加上还在用的少数几行投递记录），跑多少次都不会变大；读不了的历史会让投递停下，直到重置 outbox。授权、持久化、恢复限制和重置步骤见[记忆 outbox 说明](docs/MEMORY_OUTBOX.md)。Codex 原生记忆整理是独立的异步过程；暂存成功不代表原生记忆已经整合，也不保证某条事实会被召回。

当前索引定义有效来源快照。源文件被删除或排除后，归档计划会把确认归属且未被修改的旧副本移出日常检索目录：这次运行还可能失败时它留在本次回滚副本里，运行结束后随副本一起删除（它复制自的 Claude 记忆才是真源）。未确认归属的历史副本需要按准确哈希审阅；用户编辑会保留并报告。归属记录沿用现有 memory outbox，事务接入和回滚约定见[归档清理说明](docs/MEMORY_ARCHIVE_HYGIENE.md)。同步不会改写原生 `MEMORY.md`、记忆摘要、rollout evidence 或 SQLite 文件。

### 回滚副本

应用运行在改动任何目标之前，会把清单和每个将被改动目标的原始字节复制到 `~/.codex/claude-sync/backups/<run-id>`；指定了其他 Codex home 时使用对应位置。运行成功或回滚完成后，会删除这份临时副本。清理未完成时，`cleanup_pending` 给出残留目录，其中可能只剩部分文件，不能据此认定它还能用于回滚。自动回滚失败时不会执行清理，`recovery_required` 会指出保留的恢复材料。进程在清理前被中断也可能留下副本。从完整副本恢复：

```powershell
.\sync-all.cmd --rollback "$env:USERPROFILE\.codex\claude-sync\backups\RUN_ID" --json
```

回滚完成后同样删除这份副本。回滚只恢复当前状态仍与该次写入一致的文件或链接，保留同步后的手动编辑。outbox 历史、准备证据、授权记录和追加的记忆 note 会保留，因为删除文件不能可靠撤回 Codex 已经处理的记忆。真实配置数据和回滚副本必须位于本仓库之外。

插件被明确停用或源文件删除后，只清理本工具管理且未被手动修改的内容。手动编辑和暂时不可用的源安装目录会保留并报告。清理操作同样有回滚副本，原生记忆 note 仍只追加。

### 来源盘点与链接资源

JSON 的 `inventory` 包含入口路径、归属、来源仓库与版本、依赖声明和解释器是否存在，并列出断链恢复候选。盘点不会执行依赖脚本。只有已批准仓库中存在唯一对应来源的旧链接才会被修复：

```powershell
.\sync-all.cmd --dry-run --repair-links --approved-repo C:\src\skill-pack --json
.\sync-all.cmd --apply --repair-links --approved-repo C:\src\skill-pack --json
```

多个候选无法区分时保持原样。可以用 `--external-skill-manifest` 指定已有来源仓库清单，或设置 `CLAUDE_CONFIG_REPO`，指向配置仓根目录（读取其中的 `restore/repos.json`）；未配置时不会猜测私人仓库位置。原生角色文件和配置入口分别记录受管哈希；审阅角色的只读要求属于行为指令，运行权限仍由 Codex 当前配置决定。

链接技能的相对资源须从规范源入口解析，使用 inventory 的 `resource_context.canonical_entrypoint` 和 `resource_context.source_root`。转发包装指向已验证的上游文件，overlay 指向其受管 payload；包装、描述符或入口内容改变时，资源上下文不可用。兄弟目录资源可能需要明确批准的包根。请在已安装 `profile-sync` 的 Python 环境运行：

```powershell
python -m profile_bridge.resources --entrypoint C:\skills\example\SKILL.md --source-root C:\src\example-package --reference ../../shared-references/rules.md
```

成功时输出规范文件路径 JSON 并退出 `0`；文件缺失、绝对或远程引用、以及通过 `..` 或链接越出所选根的路径返回原因并退出 `2`。该命令不读取工作流正文、不执行资源、不改变配置；不能为通过失败检查而扩大根目录。

受管指令要求核对实际宿主能力：Claude 工具和 Cowork 动作需要宿主支持，安装成功不能证明可调用；指定人格的写作技能需要用户明确意图。原始技能的 YAML frontmatter 必须含唯一映射键及非空字符串名称、描述。适配器从解析后的标量生成描述，保留比较条件和内联触发语，示例正文仍留在源工作流。Inventory 保留 catalog 源哈希和插件版本；Git 来源不可用时如实报告。

运行时选择按 selection snapshot 中的哈希验证所选替代项的每个生成文件，包括指令、描述符和 payload。缺失或修改会阻止选择并保留文件。旧快照若无文件哈希，需要先通过正常 profile plan 重新生成。

### 计划运行与健康检查

现有任务调度器可以调用确定性入口：

```powershell
python .\run_profile_sync.py --apply --write-status --probe-mcp
```

它先应用受管配置，再重新计算计划，检查未解决的冲突和缺失依赖。退出码 `0` 表示检查通过，`2` 表示存在待处理问题或剩余变更，`1` 表示执行失败。JSON 将兼容性排除项与故障分开列出。本地 HTTP MCP 只做初始化，不调用工具；外部服务标为 `not_checked`，stdio 服务只检查启动命令是否存在。

`--accepted-findings <文件>` 指定一份审过的常驻问题基线，比如因能力未验证而阻塞的 skill、待审的 hook。这份文件放在私有配置里，不能进公开仓。每条是 `{"identity": {...}, "count": n}`，identity 由问题的 area、status、reason 和对象（名称、hook 事件、告警来源、能力原因、被跳过的记忆文件）组成，不看提示文字。所有问题都在基线里时状态为 `accepted`，退出码 `0`。基线之外的问题，包括超出已接受次数的重复项，仍然退出 `2`，并列在 `new_findings` 里。待处理变更不能被接受；基线格式错误按执行失败处理（`1`）。不再命中的条目列在 `stale_accepted`，问题解决后可以从基线里删掉。

`--write-status` 会在所选 Codex home 下写入 `claude-sync/last-run.json`。运行期间保留上次的 `last-success.json`，只有复查通过且状态为 `healthy` 或 `accepted` 才更新成功标记。保留的标记说明的是上次成功运行；任务监控还应检查本次退出码和运行标识。入口不发送消息、不创建任务，由现有调度、监控和备份体系统一管理。

hook 告警的 identity 也包含事件名。保留的归档副本通常只按不同的保留原因区分，同类副本增加仍可沿用原基线。若某个副本检出凭据特征，它的原因、路径和 SHA256 还会进入 `detail.archive_credentials`；新增凭据问题、内容变化或涉及另一个文件，都需要重新审查。缺少这些对象字段的旧基线不能接受相应问题。

同步和健康检查本身不调用模型。自动化流程需要 agent 时，应通过已有 `llmcall` 接口的 `mode="agent"` 调用，沿用其路由策略。

执行工作流需要提供 0.3.0 调用接口的 `llmcall` 客户端；更早的客户端会在写入工作流状态之前报错。llmcall 的子进程运行在调用方进程的工作目录和环境里，所以工作流只能在锚定的工作目录中继续。从其他目录发起继续请求会报 `workspace_requires_process_cwd`，需要先切换到锚定目录；llmcall 0.2.0 则由每次调用传入该目录。幂等读取已经完成的请求仍可在任意目录进行。为 llmcall 0.2.0 写下的历史记录仍可读取和继续。

## Runtime generation

`tools/` 负责构建、安装和启动同步所用的隔离 runtime generation（`~/.codex/claude-sync/runtime-artifacts`）。构建清单和版本钉属于使用者自己的配置，放在私有仓库里。详见 [runtime generation 说明](docs/runtime-generation.md)。

## 局限

- `extensions\ad_hoc\instructions.md` 是从本机 Codex 安装中检测到的约定，不是公开保证稳定的 API。如果该文件缺失，或未来 Codex 变更使约定失效，投递会停下（记忆报告显示 `unsupported`），桥接可能需要更新。
- 同步仍是单向的，Codex consolidation 仍是异步的；立即或保证召回不在本工具范围内。
- 凭据检测属于启发式保护，仍然需要阅读 dry-run 结果，并确保来源内容经过审查且不含敏感信息。

## 语言

English（[`README.md`](README.md)，权威版本）· 中文（`README_CN.md`）

## 许可证

[MIT](LICENSE)
