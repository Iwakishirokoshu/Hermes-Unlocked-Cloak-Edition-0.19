@echo off
setlocal EnableExtensions
call "%~dp0config.bat" || exit /b 1
if not exist "%SSH_KEY%" (
  echo SSH key was not found: %SSH_KEY%
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0repair_key_permissions.ps1" -KeyPath "%SSH_KEY%"
if errorlevel 1 (
  echo Could not repair SSH key permissions.
  exit /b 1
)
echo SSH key permissions are ready.
endlocal