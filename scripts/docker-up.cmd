@echo off
rem Thin wrapper so scripts\docker-up.ps1 runs even under a restrictive
rem PowerShell execution policy -- -ExecutionPolicy Bypass only applies to
rem this one invocation, it does not change any system setting.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0docker-up.ps1" %*
