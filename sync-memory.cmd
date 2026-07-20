@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0sync-claude-memory-to-codex.ps1" %*
set "SYNC_EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %SYNC_EXIT_CODE%
