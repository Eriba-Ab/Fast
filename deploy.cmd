@echo off
rem Runs deploy.ps1 without changing the machine's PowerShell execution policy.
rem Usage: deploy            (draft deploy)
rem        deploy -Prod      (production)
rem        deploy -BuildOnly (build and stage dist\ only)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" %*
