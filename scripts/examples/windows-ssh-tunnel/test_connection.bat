@echo off
setlocal EnableExtensions
call "%~dp0config.bat" || exit /b 1
call "%~dp0repair_key_permissions.bat" || exit /b 1
set "SSH_EXE=%SystemRoot%\System32\OpenSSH\ssh.exe"
if not exist "%SSH_EXE%" set "SSH_EXE=ssh"
"%SSH_EXE%" -i "%SSH_KEY%" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new %VPS_USER%@%VPS_HOST% "set -e; echo Dashboard-service:; systemctl is-active hermes-dashboard.service; echo Docker:; systemctl is-active docker; echo Nginx:; systemctl is-active nginx; echo Dashboard-HTTP:; curl -sS -o /dev/null -w '%%{http_code}\n' http://127.0.0.1:9119/; echo Cloak-Manager:; docker ps --filter name=^/cloakbrowser-manager$ --format '{{.Names}} {{.Status}}'"
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" echo VPS check failed with code %RESULT%.
pause
exit /b %RESULT%