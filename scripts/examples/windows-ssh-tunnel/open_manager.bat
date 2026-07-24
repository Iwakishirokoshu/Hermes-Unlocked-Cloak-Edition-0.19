@echo off
setlocal EnableExtensions
call "%~dp0config.bat" || exit /b 1
call "%~dp0repair_key_permissions.bat" || exit /b 1
set "SSH_EXE=%SystemRoot%\System32\OpenSSH\ssh.exe"
if not exist "%SSH_EXE%" set "SSH_EXE=ssh"
if not defined NO_BROWSER (
  start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://127.0.0.1:%LOCAL_CLOAK_MANAGER_PORT%'"
)
echo Cloak Manager: http://127.0.0.1:%LOCAL_CLOAK_MANAGER_PORT%
echo Keep this window open while using the private tunnel.
"%SSH_EXE%" -i "%SSH_KEY%" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L %LOCAL_CLOAK_MANAGER_PORT%:127.0.0.1:8080 %VPS_USER%@%VPS_HOST% -N
set "RESULT=%ERRORLEVEL%"
echo SSH tunnel stopped with code %RESULT%.
pause
exit /b %RESULT%