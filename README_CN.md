# Claude Code → Codex 记忆同步

用一条本地 PowerShell 命令，把 Claude Code 的项目自动记忆转换为本机 Codex `ad_hoc` staging note。

[![测试](https://github.com/DaizeDong/claude-codex-memory-sync/actions/workflows/test.yml/badge.svg)](https://github.com/DaizeDong/claude-codex-memory-sync/actions/workflows/test.yml)
[![PowerShell 5.1](https://img.shields.io/badge/Windows%20PowerShell-5.1-5391FE?logo=powershell&logoColor=white)](sync-claude-memory-to-codex.ps1)
[![许可证：MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![语言](https://img.shields.io/badge/Languages-EN%20%2F%20CN-blue?style=flat)](#语言)

[English](README.md) | [中文版](README_CN.md)

> 这是独立的社区项目，与 Anthropic 或 OpenAI 无隶属或官方背书关系。

## ⭐ 请先阅读：设计哲学

**暂存可验证的记忆更新，不假装两个 Agent 共享同一颗脑。**

这个工具有意保持小而简单。它遵循三条原则：

1. **只填补缺失的接口，不增加新的 Agent 交互面。** Claude Code 已经保存项目记忆，Codex 也已经负责自己的记忆整理；本工具只把一种表示转换为本机检测到的入口约定所接受的格式。它不增加 Agent 终端、daemon、MCP server、数据库、向量库或模型调用。
2. **通过本机检测到的入口约定暂存，不伪装成 Codex 内部组件。** 脚本只向 `extensions\ad_hoc\notes\` 追加自包含 note，不改写 Codex 的记忆摘要、rollout evidence 或 SQLite 状态。“同步成功”只表示“已安全暂存”，不表示“已经整理或保证会被召回”。
3. **记忆质量比记忆数量更重要。** 先 dry run、保守筛选、发现疑似凭据时 fail closed、限制所有输入规模，并进行增量去重。把所有日志和过期决策都复制过去，只会让记忆池更大，甚至可能让召回质量更差。

设计目标是成为两个现有记忆系统之间最小、可审计的桥，而不是通用共享记忆平台。

## 它是什么（以及不是什么）

`claude-codex-memory-sync` 是面向 Windows PowerShell 5.1 的轻量、本地、单向转换器。它读取 Claude Code 的项目自动记忆 Markdown，执行路径与凭据检查，再把符合条件的内容暂存为 Codex `ad_hoc` note。

```text
Claude project memory (*.md)
        │ 只读筛选、凭据扫描、增量去重
        ▼
<CodexMemoriesRoot>\extensions\ad_hoc\notes\*.md
        │ 由 Codex 自己异步处理
        ▼
Codex memory consolidation
```

它不会：

- 启动新的 Agent 终端或后台服务；
- 安装 MCP server、数据库或向量库；
- 在运行时调用模型、`codex exec` 或任何网络 API；
- 从 Codex 向 Claude 回写记忆；
- 把两套原生记忆变成强一致数据库；
- 保证 Codex 会在何时、以何种方式或是否召回某条暂存内容。

## 安装

运行要求：

- Windows；
- Windows PowerShell 5.1（`powershell.exe`），这是已支持并经过测试的运行时；
- Claude memory 目录存在并包含 `MEMORY.md`；
- Codex memories 已启用，且目标 memories 根目录存在并包含 `extensions\ad_hoc\instructions.md`。

克隆仓库即可；工具不需要额外 PowerShell module 或 package。Git 用于克隆，并在可用时用于发现 Git 根目录：

```powershell
git clone https://github.com/DaizeDong/claude-codex-memory-sync.git
Set-Location .\claude-codex-memory-sync
```

## 快速开始

第一次先做零写入预览：

```powershell
.\sync-memory.cmd -ProjectPath "C:\path\to\your-project" -DryRun
```

核对选中文件数、字节数、计划变更和安全检查结果。如果自动来源映射存在歧义，请停止写入并显式传入 `-ClaudeMemoryPath`。确认预览无误后再暂存 note：

```powershell
.\sync-memory.cmd -ProjectPath "C:\path\to\your-project"
```

也可以直接调用 PowerShell 脚本：

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\sync-claude-memory-to-codex.ps1 `
  -ProjectPath "C:\path\to\your-project" `
  -DryRun
```

包装器使用的 `-ExecutionPolicy Bypass` 只作用于这一次 PowerShell 进程，不会修改系统或用户的持久执行策略。`sync-memory.cmd` 会原样透传参数和脚本退出码。

## 工作方式

默认 Codex memories 根目录依次为：

1. 已设置 `CODEX_HOME` 时使用 `$env:CODEX_HOME\memories`；
2. 否则使用 `%USERPROFILE%\.codex\memories`。

脚本只写入 `extensions\ad_hoc\notes\`，不会直接修改 `MEMORY.md`、`memory_summary.md`、`raw_memories.md`、rollout evidence 或 Codex 的 SQLite 状态。

同步采用单向追加/更新语义。删除或重命名 Claude 源文件，不会删除、重命名或撤回已经为 Codex 暂存的记忆。需要纠正或遗忘旧记忆时，应显式要求 Codex 处理；只删除 Claude 源文件并不足够。

“已暂存”也不等于“已整理”。Codex 会按自己的节奏异步处理 ingress note。

## 路径发现与显式覆盖

默认值：

- `ProjectPath`：当前工作目录。
- `ClaudeProjectsRoot`：`%USERPROFILE%\.claude\projects`。
- `CodexMemoriesRoot`：优先 `$env:CODEX_HOME\memories`；未设置 `CODEX_HOME` 时为 `%USERPROFILE%\.codex\memories`。

当 Git 可用且针对 `ProjectPath` 的 `git rev-parse` 成功时，Git 根目录用于项目身份和自动映射。否则，自动发现会从 `ProjectPath` 及其父目录查找，并要求恰好一个 Claude memory 匹配。路径编码可能产生歧义，移动过的仓库也可能留下旧目录；移动项目还会改变项目身份。摘要会有意隐藏自动发现的完整路径。不能确定映射时不要写入，请改用显式覆盖。

指定 Claude project key：

```powershell
.\sync-memory.cmd -ProjectPath "D:\src\app" `
  -ClaudeProjectsRoot "D:\claude\projects" `
  -ClaudeProjectKey D--src-app `
  -DryRun
```

或者直接指定 Claude memory 目录和 Codex memories 根目录：

```powershell
.\sync-memory.cmd -ProjectPath "D:\src\app" `
  -ClaudeMemoryPath "D:\claude\projects\D--src-app\memory" `
  -CodexMemoriesRoot "D:\codex\memories" `
  -DryRun
```

`ClaudeProjectKey` 与 `ClaudeMemoryPath` 是互斥的来源模式。`ClaudeMemoryPath` 只覆盖来源目录；项目身份仍来自 Git 根目录或 `ProjectPath`。参数或路径无效时，脚本以退出码 `1` 结束。

## 参数

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `-ProjectPath <path>` | 当前目录 | 当前仓库/项目路径，用于来源映射和项目身份。 |
| `-ClaudeProjectsRoot <path>` | `%USERPROFILE%\.claude\projects` | Claude Code projects 根目录。 |
| `-ClaudeProjectKey <name>` | 自动发现 | 显式指定 Claude project 目录名。 |
| `-ClaudeMemoryPath <path>` | 自动发现 | 直接指定 Claude memory 目录，适合非标准布局或映射歧义。 |
| `-CodexMemoriesRoot <path>` | `$env:CODEX_HOME\memories`，否则 `%USERPROFILE%\.codex\memories` | Codex memories 根目录；用于便携、测试或非默认安装。 |
| `-DryRun` | 关闭 | 完成发现、筛选、安全检查和计划输出，但不写 staging note。 |
| `-IncludeReadme` | 关闭 | 加入 memory 根目录中 basename 按大小写不敏感精确匹配 `README.md` 的文件；默认排除以减少说明文字噪声。 |
| `-IncludeArchive` | 关闭 | 额外加入 `memory\archive\*.md` 的直接子级；默认排除归档材料。 |
| `-IncludeSensitiveNames` | 关闭 | 允许文件名命中敏感规则的候选继续接受内容扫描。它**绝不会**绕过凭据扫描。 |
| `-MaxFileBytes <int>` | `65536` | 单个来源文件上限；有效范围为 1 KiB–1 MiB。 |
| `-MaxTotalBytes <int>` | `4194304` | 候选内容累计上限；必须不小于 `MaxFileBytes` 且不超过 64 MiB。 |
| `-LockTimeoutSeconds <int>` | `10` | 等待全局目标锁的时间；有效范围为 0–300 秒。 |
| `-OutputFormat Text\|Json` | `Text` | 人类可读输出或机器可读 JSON。 |

单个快照最多选择 500 个来源文件。历史读取另有独立上限：当前项目最多 4,096 个 note、累计 64 MiB，且单个 note 不超过 4 MiB。

JSON 预览示例：

```powershell
.\sync-memory.cmd -ProjectPath "D:\src\app" -DryRun -OutputFormat Json
```

| `status` | 含义 |
|---|---|
| `preview` | dry run 发现待新增或更新的内容。 |
| `staged` | 至少一个 note 已暂存。 |
| `no_changes` | 没有变化，属于成功。 |
| `blocked` | 预检安全规则阻断整批操作。 |
| `error` | 发生由脚本捕获的失败；也可能表示最终生成 note 的安全检查拒绝。 |

正常结果和预检安全阻断会使用完整 JSON schema：`tool`、`version`、`status`、`dry_run`、`project_id`、`selected_files`、`selected_bytes`、`added`、`updated`、`unchanged`、`blocked`、`notes_written`、`partial_write`、`blocked_items`、`consolidation` 和 `deletes_propagated`。参数绑定成功后由脚本捕获的运行时失败——包括只在最终 note envelope 构造完成后发现的安全拒绝——会使用字段较少的 `status: "error"` schema：`tool`、`version`、`status`、`message`、`notes_written`、`partial_write` 和 `consolidation`。PowerShell 启动、解析和参数绑定错误发生在脚本 formatter 之前，可能输出原生 stderr 而不是 JSON。应始终以进程退出码为准。

## 默认筛选与安全模型

默认选择 Claude memory 根目录的直接 `*.md` 子级，不递归，并按大小写不敏感的精确文件名匹配排除 `README.md`。`-IncludeArchive` 会额外选择 `memory\archive\*.md` 的直接子级。reparse point、UNC/device/alternate-data-stream 路径、无法安全解析的路径和非普通文件不会被视为可信来源。

读取器接受严格 UTF-8、带 BOM 的 UTF-8，以及带 BOM 的 UTF-16 LE/BE；它把支持的行分隔符规范化为 LF，并把文本规范化为 NFC，同时拒绝 NUL 与危险控制字符。脚本还会检查来源在读取期间保持稳定；来源不稳定或编码无效时，会在写入新 note 前以退出码 `1` 失败。

安全检查分两层：

1. **敏感文件名检查。** 命中后默认阻断整批写入。`-IncludeSensitiveNames` 只允许该文件继续接受内容扫描。
2. **硬凭据/secret 内容检查。** 疑似私钥、访问令牌或其他硬凭据会阻断整批写入。任何 include 参数都不能绕过这一层。

安全阻断采用 all-or-nothing：退出码为 `2`、零写入，不会留下部分 staging note。这些检查用于降低意外泄露风险，但属于启发式保护，不是完整 secret scanner。请始终阅读 dry-run 结果，也不要把密码、token、私钥、个人数据或受监管数据放进 Agent memory。

导入的 Claude 文本会被包装为引用形式的不可信数据，并明确标记为不可执行内容。这可以降低指令混淆，但不代表应该同步敌意或无关内容。

为了保留作用域，note 会保存项目绝对路径。因此，路径中的用户名、客户名和目录结构也会成为记忆内容。如果路径本身敏感，请使用中性项目路径，并在需要时通过 `-ClaudeMemoryPath` 显式定位来源。

## 增量语义与信任边界

重复运行会比较来源身份和内容状态，不会再次暂存未变化的内容。来源变化时会追加新的 update note，而不会原地修改旧 note。`no_changes` 属于成功结果。

单个 note 采用原子发布，但多 note 批次不是文件系统事务。如果 I/O 错误中断批次，已经发布的 note 会保留。JSON 会准确报告 `partial_write=true` 和实际 `notes_written`；修复底层问题后重跑，工具会跳过这些 note，并继续其余变更。

为了建立增量基线，工具会从目标目录读取当前项目的有效 CCMS 历史 note；其他项目和非 CCMS note 不属于这条基线。按目标目录派生的 `Global\` mutex 会串行化指向同一目标的本工具协作实例。能够写入该目录的本地账号或进程位于同一信任边界。结构检查、current/previous 快照哈希、元数据、文件名、时间戳和版本链校验可以发现损坏，但不能认证同权限本地进程的写入。note 内容是数据，不是可执行指令。

本工具不会传播：

- Codex 变更向 Claude 的回写；
- Claude 端的删除；
- 来源重命名对应的 Codex 端重命名或撤回——重命名会成为新来源，而旧来源仍然保留；
- 两端已有记忆之间的语义冲突自动解决。

这不是 Claude 与 Codex 原生记忆的等价转换。Codex 在 consolidation 时可能摘要、改写、遗漏暂存内容，或让其与既有记忆冲突。更新 note 会携带 previous/current 快照和 superseded 标记，它保留了版本关系，但仍可能增加少量噪声。

必须稳定执行的项目规则应放在 `AGENTS.md` 或仓库文档中。记忆应作为辅助召回层，而不是唯一事实来源。

## 退出码

| 退出码 | 含义 |
|---:|---|
| `0` | 正常完成，包括 `staged`、`preview` 或 `no_changes`。 |
| `1` | 参数、路径、编码、大小、历史、锁、I/O 或内部处理的致命错误。使用 JSON 时检查 `partial_write` 和 `notes_written`。 |
| `2` | 因硬 secret 或未显式放行的敏感文件名触发安全拒绝；整批零写。预检拒绝通常使用 `status: "blocked"`，最终生成 note 扫描则可能使用 `status: "error"`。 |

保留准确退出码的 Windows 批处理示例：

```bat
call sync-memory.cmd -ProjectPath "D:\src\app" -DryRun -OutputFormat Json
set "SYNC_CODE=%ERRORLEVEL%"
if "%SYNC_CODE%"=="2" echo Safety block: nothing was written.
if not "%SYNC_CODE%"=="0" if not "%SYNC_CODE%"=="2" echo Fatal error.
exit /b %SYNC_CODE%
```

复杂自动化建议解析 JSON 对象。

Dry run 不执行写入，但仍会验证来源、ingress 约定、安全规则和现有历史，因此也可能返回 `1` 或 `2`。

## 验证与测试

在仓库根目录运行完整黑盒测试：

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass `
  -File .\tests\run-tests.ps1
```

然后针对真实本地配置做一次零写入预览：

```powershell
.\sync-memory.cmd `
  -ProjectPath "C:\path\to\your-project" `
  -DryRun `
  -OutputFormat Json
```

`-DryRun` 不创建 notes 目录，也不写 staging note，但仍会验证真实目标中的 `extensions\ad_hoc\instructions.md`。不要把 `CodexMemoriesRoot` 指向一个没有该入口约定的空临时目录；隔离测试请使用随附的黑盒测试套件。

## 局限

- 当前版本只支持 Windows 上的 Windows PowerShell 5.1。PowerShell 7 和其他操作系统不是已测试目标。
- `extensions\ad_hoc\instructions.md` 是从本机 Codex 安装中检测到的约定，不是公开保证稳定的 API。如果该文件缺失，或未来 Codex 变更使约定失效，工具会 fail closed 并以退出码 `1` 结束，且可能需要更新。
- 同步仍是单向的，Codex consolidation 仍是异步的；立即或保证召回不在本工具范围内。
- 凭据检测属于启发式保护，仍然需要阅读 dry-run 结果，并确保来源内容经过审查且不含敏感信息。

## 语言

English（[`README.md`](README.md)，权威版本）· 中文（`README_CN.md`）

## 许可证

[MIT](LICENSE)
