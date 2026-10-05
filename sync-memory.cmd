@echo off
setlocal
rem Compatibility arguments and exit status flow to the canonical Python core.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0sync-claude-memory-to-codex.ps1" %*
set "SYNC_EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %SYNC_EXIT_CODE%
