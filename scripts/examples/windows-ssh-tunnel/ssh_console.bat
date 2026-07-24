@echo off
setlocal EnableExtensions
call "%~dp0config.bat" || exit /b 1
call "%~dp0repair_key_permissions.bat" || exit /b 1
set "SSH_EXE=%SystemRoot%\System32\OpenSSH\ssh.exe"
if not exist "%SSH_EXE%" set "SSH_EXE=ssh"
"%SSH_EXE%" -i "%SSH_KEY%" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new %VPS_USER%@%VPS_HOST%
endlocal