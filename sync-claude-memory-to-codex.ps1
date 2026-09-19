#requires -Version 5.1

[CmdletBinding(DefaultParameterSetName = 'Auto')]
param(
    [Parameter(Position = 0)]
    [string]$ProjectPath = (Get-Location).Path,
    [string]$ClaudeProjectsRoot,
    [Parameter(Mandatory = $true, ParameterSetName = 'Key')]
    [string]$ClaudeProjectKey,
    [Parameter(Mandatory = $true, ParameterSetName = 'Direct')]
    [string]$ClaudeMemoryPath,
    [string]$CodexMemoriesRoot,
    [switch]$DryRun,
    [switch]$IncludeReadme,
    [switch]$IncludeArchive,
    [switch]$IncludeSensitiveNames,
    [long]$MaxFileBytes = 65536,
    [long]$MaxTotalBytes = 4194304,
    [int]$LockTimeoutSeconds = 10,
    [ValidateSet('Text', 'Json')]
    [string]$OutputFormat = 'Text'
)

# Compatibility shell. Python owns validation, history, locking and publication.
$ErrorActionPreference = 'Stop'
$python = if ($env:CCMS_PYTHON) { $env:CCMS_PYTHON } else { 'python' }
$arguments = @('-m', 'profile_bridge.memory.compat')
foreach ($entry in $PSBoundParameters.GetEnumerator()) {
    if ($entry.Value -is [System.Management.Automation.SwitchParameter]) {
        if ($entry.Value.IsPresent) { $arguments += ('-' + $entry.Key) }
    } else {
        $arguments += ('-' + $entry.Key)
        $arguments += [string]$entry.Value
    }
}
if (-not $PSBoundParameters.ContainsKey('ProjectPath')) {
    $arguments += @('-ProjectPath', $ProjectPath)
}
try {
    & $python @arguments
    exit $LASTEXITCODE
} catch {
    if ($OutputFormat -eq 'Json') {
        [Console]::Out.WriteLine('{"tool":"claude-codex-memory-sync","version":"1.0.0","status":"error","message":"Canonical memory core is unavailable.","notes_written":0,"partial_write":false,"consolidation":"not_requested"}')
    } else { [Console]::Error.WriteLine('CCMS error: Canonical memory core is unavailable.') }
    exit 1
}