@echo off
setlocal
python -B "%~dp0profile_sync.py" %*
set "SYNC_EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %SYNC_EXIT_CODE%
