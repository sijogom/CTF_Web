@echo off
title CTF Platform - Status
set "SCRIPT_DIR=%~dp0"
set "INFRA_DIR=%SCRIPT_DIR%.."
for %%I in ("%INFRA_DIR%") do set "INFRA_ABS=%%~fI"
wsl bash -c "cd \"$(wslpath '%INFRA_ABS%')\" && bash scripts/server.sh status"
pause
