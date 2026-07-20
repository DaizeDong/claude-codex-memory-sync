# Claude Code → Codex 记忆同步

这是一个轻量、单向、本地运行的 PowerShell 工具。它读取 Claude Code 的 project auto-memory，把可安全同步的 Markdown 内容转换为 Codex 的 `ad_hoc` ingress note。

它不是新的 Agent 终端，也不安装或启动 daemon、MCP server、数据库、向量库；运行时不调用模型、`codex exec` 或网络 API。

> 这是社区工具，与 Anthropic 或 OpenAI 无隶属或官方背书关系。

## 工作边界

数据流如下：

```text
Claude project memory (*.md)
        │ 只读、筛选、凭据扫描、增量去重
        ▼
<CodexMemoriesRoot>\extensions\ad_hoc\notes\*.md
        │ Codex 后续异步处理
        ▼
Codex memory consolidation
```

默认 Codex memories 根目录优先使用 `$env:CODEX_HOME\memories`；未设置 `CODEX_HOME` 时回退到 `%USERPROFILE%\.codex\memories`。

脚本只向其 `extensions\ad_hoc\notes\` 追加 staging note，不直接改写 `MEMORY.md`、`memory_summary.md`、`raw_memories.md`、rollout evidence 或 Codex 的 SQLite 状态。

“同步成功”表示 note 已安全暂存，不表示 Codex 已经完成 consolidation，也不保证下一次对话会立即召回。Codex 的整理是异步的，具体时机由 Codex 自己决定。

同步是单向追加/更新语义。Claude 端删除或重命名文件，不会删除或重命名已经进入 Codex 的记忆；需要遗忘或纠正时，应在 Codex 中显式提出。

## 快速开始

需要 Windows PowerShell 5.1 或更高版本，并且 Codex memories 已启用、目标根目录中存在 `extensions\ad_hoc\instructions.md`。第一次务必先运行 dry run。

以下命令在 Windows PowerShell 中运行：

```powershell
Set-Location C:\path\to\claude-codex-memory-sync
.\sync-memory.cmd -ProjectPath C:\path\to\your-project -DryRun
```

核对候选数量、字节数和安全扫描结果；自动映射存在疑问时先改用显式 `-ClaudeMemoryPath`。确认无误后再实际暂存：

```powershell
.\sync-memory.cmd -ProjectPath C:\path\to\your-project
```

也可以直接调用 PowerShell 脚本：

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\sync-claude-memory-to-codex.ps1 `
  -ProjectPath C:\path\to\your-project `
  -DryRun
```

`sync-memory.cmd` 使用的 `-ExecutionPolicy Bypass` 只作用于这一次 PowerShell 进程，不会修改系统或用户的持久执行策略。包装器会原样透传参数和脚本退出码。

## 路径发现与显式覆盖

默认值：

- `ProjectPath`：当前工作目录。
- `ClaudeProjectsRoot`：`%USERPROFILE%\.claude\projects`。
- `CodexMemoriesRoot`：优先 `$env:CODEX_HOME\memories`；`CODEX_HOME` 未设置时为 `%USERPROFILE%\.codex\memories`。

脚本会根据项目绝对路径查找对应的 Claude project memory。路径编码可能产生歧义，移动过的仓库也可能保留旧目录；为避免泄露本机路径，摘要输出不会打印自动发现的完整路径。不能确定映射时，不要继续写入，改用显式覆盖。

指定 Claude project key：

```powershell
.\sync-memory.cmd -ProjectPath D:\src\app `
  -ClaudeProjectsRoot D:\claude\projects `
  -ClaudeProjectKey D--src-app `
  -DryRun
```

或者直接指定 memory 目录和 Codex memories 根目录：

```powershell
.\sync-memory.cmd -ProjectPath D:\src\app `
  -ClaudeMemoryPath D:\claude\projects\D--src-app\memory `
  -CodexMemoriesRoot D:\codex\memories `
  -DryRun
```

`ClaudeMemoryPath` 是最明确的来源覆盖。不要同时传入互相冲突的来源定位参数；参数或路径无效时脚本会以退出码 `1` 结束。

## 参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `-ProjectPath <path>` | 当前目录 | 当前仓库/项目目录；用于来源映射和项目身份。 |
| `-ClaudeProjectsRoot <path>` | `%USERPROFILE%\.claude\projects` | Claude Code projects 根目录。 |
| `-ClaudeProjectKey <name>` | 自动发现 | 显式指定 Claude 的 project 目录名。 |
| `-ClaudeMemoryPath <path>` | 自动发现 | 直接指定 Claude memory 目录，适合非标准布局或映射歧义。 |
| `-CodexMemoriesRoot <path>` | `$env:CODEX_HOME\memories`，否则 `%USERPROFILE%\.codex\memories` | Codex memories 根目录；测试、便携环境或非默认安装时覆盖。 |
| `-DryRun` | 关闭 | 完成发现、筛选、安全检查和计划输出，但不写 staging note。建议每次更换项目或规则时先使用。 |
| `-IncludeReadme` | 关闭 | 加入 memory 根目录中恰好名为 `README.md` 的文件；默认排除以避免说明文字污染长期记忆。 |
| `-IncludeArchive` | 关闭 | 额外加入 `memory\archive\*.md` 的直接子级；默认排除历史材料。 |
| `-IncludeSensitiveNames` | 关闭 | 允许文件名命中敏感名称规则的候选继续接受内容扫描。它**不会**绕过凭据扫描。 |
| `-MaxFileBytes <int>` | `65536` | 单个来源文件的最大字节数。 |
| `-MaxTotalBytes <int>` | `4194304` | 单次候选内容的累计最大字节数。 |
| `-LockTimeoutSeconds <int>` | `10` | 等待全局同步锁的最长秒数，防止不同进程或 Windows session 相互覆盖。 |
| `-OutputFormat Text\|Json` | `Text` | 人类可读输出或便于自动化解析的 JSON 输出。 |

示例：为 CI 或其他脚本输出 JSON 预览：

```powershell
.\sync-claude-memory-to-codex.ps1 -ProjectPath D:\src\app -DryRun -OutputFormat Json
```

## 默认筛选与安全模型

默认扫描 memory 根目录直接子级的 `*.md`，不递归，其中只排除恰好名为 `README.md` 的文件；`-IncludeArchive` 才会额外加入 `memory\archive\*.md` 的直接子级。敏感文件名会触发整批阻断而不是被静默排除，超出大小上限则是致命错误。符号链接/reparse point、无法安全解析的路径和非普通文件不会被当作可信来源。任何扩大输入面的 include 参数都应在 dry run 后再启用。

安全检查分两层：

1. **敏感文件名检查**：默认阻断整批写入。只有 `-IncludeSensitiveNames` 可以明确放行这类名称，使文件继续接受内容扫描。
2. **硬凭据/secret 内容检查**：发现疑似私钥、访问令牌或其他硬凭据时阻断整批写入。任何 include 参数都不能绕过它。

安全阻断采用 all-or-nothing：退出码为 `2`，本次写入数量为零，不会留下部分 staging note。它是降低误同步风险的启发式保护，不是秘密扫描器的完备替代品。提交前仍应阅读 dry run 输出，不要把密码、token、私钥、个人隐私或受监管数据放入 Agent memory。

默认排除和最大字节数不仅用于安全，也用于控制记忆噪声。放入过多日志、重复说明和过期决策，可能降低后续召回质量。

为限定作用域，note 会保存项目绝对路径；路径中的用户名、客户名和目录结构也会成为记忆内容。路径敏感时，请先使用中性项目路径，并按需通过 `-ClaudeMemoryPath` 显式定位来源。

## 增量同步语义

重复运行会根据来源和内容状态避免重复暂存没有变化的内容。没有新内容时属于正常 no-change，不是错误。

单个 note 采用原子发布，但整批同步不是事务。若中途发生 I/O 错误，已发布的 note 会保留，JSON 会以 `partial_write=true` 和实际 `notes_written` 报告；修复问题后重跑，增量去重会跳过它们并继续其余变更。

脚本把目标 `notes` 目录中符合 CCMS 格式的当前项目历史 note 当作增量基线；能写入该目录的本地账号或进程属于同一信任边界。结构、previous/current 快照哈希和版本链关系校验可发现损坏，但不提供对同权限本地恶意写入的认证；这里的信任仅指同步状态，不表示把 note 内容当作可执行指令。

为避免无界读取，当前项目的历史基线上限为 4096 个 note、累计 64 MiB，且单个 note 不超过 4 MiB；超过时以退出码 `1` 失败并保持零新增写入。

这个工具不会把 Claude 和 Codex 变成强一致的双向数据库：

- 不从 Codex 回写 Claude。
- 不传播 Claude 端的删除。
- 不传播重命名为 Codex 端的重命名/撤回。
- 不自动解决两边已有记忆的语义冲突。
- 不保证 Codex consolidation 的完成时间或最终表述。

如果某条旧记忆已经错误，应显式要求 Codex 更新或遗忘，而不是仅删除 Claude 源文件。

这不是 Claude 与 Codex 原生记忆的等价转换。Claude 内容会作为 `ad_hoc` note 由 Codex 再整理，可能被摘要、改写、漏召回或与既有记忆冲突；更新 note 同时携带 previous/current 快照，虽然有 superseded 标记，仍可能增加少量噪声。

必须稳定执行的规则应放在 `AGENTS.md` 或仓库文档中，记忆只作为辅助召回层。

## 退出码

| 退出码 | 含义 |
|---:|---|
| `0` | 正常完成，包括成功写入、dry run 和 no-change。 |
| `1` | 致命错误，例如参数、路径、I/O、锁或内部处理失败；检查 JSON 的 `partial_write` 与 `notes_written` 判断极端 I/O 失败前是否已有 note 暂存。 |
| `2` | 安全阻断：检测到硬 secret，或检测到未显式放行的敏感文件名；整批零写。 |

批处理脚本示例：

```bat
call sync-memory.cmd -ProjectPath D:\src\app -DryRun -OutputFormat Json
set "SYNC_CODE=%ERRORLEVEL%"
if "%SYNC_CODE%"=="2" echo Safety block: nothing was written.
if not "%SYNC_CODE%"=="0" if not "%SYNC_CODE%"=="2" echo Fatal error.
exit /b %SYNC_CODE%
```

先保存 `%ERRORLEVEL%` 再做等值判断，可避免退出码 `2` 同时落入一般错误分支；复杂自动化建议解析 JSON 输出。

## 验证与测试

在工具目录运行完整测试：

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\tests\run-tests.ps1
```

再针对当前真实配置做一次零写入手工预览：

```powershell
.\sync-claude-memory-to-codex.ps1 `
  -ProjectPath C:\path\to\your-project `
  -DryRun `
  -OutputFormat Json
```

`-DryRun` 不创建 notes 目录，也不写任何 staging note，但仍会验证真实目标中的 `extensions\ad_hoc\instructions.md`。因此不要把 `CodexMemoriesRoot` 指向一个没有该入口的空临时目录；需要隔离测试时，请运行随附的黑盒测试套件。

## 许可证

MIT，见 [LICENSE](LICENSE)。
