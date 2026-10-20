<#
.SYNOPSIS
    Build on this PC and publish the site, the staff console and the data to the VPS.

.DESCRIPTION
    1. Runs build.py (staff console, public page, assessments export into build\).
    2. Copies server\, deploy\ (without .env) and build\ to the VPS over SSH (scp).
    3. Rebuilds and restarts the containers, then loads the notices into the database.

    Needs OpenSSH (built into Windows 10/11) and an SSH key already set up for the VPS.
    deploy\.env is never copied: create it on the VPS itself (see deploy\README.md).

.EXAMPLE
    .\deploy\publish.ps1 -Server deploy@203.0.113.10
.EXAMPLE
    .\deploy\publish.ps1 -Server deploy@203.0.113.10 -SkipSync    # site/code only
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$Server,          # user@host of the VPS
    [string]$RemoteDir = '/opt/tirs',
    [switch]$SkipBuild,
    [switch]$SkipSync
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
function Step($t) { Write-Host "`n==> $t" -ForegroundColor Green }
function Fail($t) { Write-Host "`nERROR: $t" -ForegroundColor Red; exit 1 }

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) { Fail 'OpenSSH (ssh/scp) was not found. Enable "OpenSSH Client" in Windows Optional Features.' }

if (-not $SkipBuild) {
    Step 'Building'
    & python (Join-Path $root 'build.py')
    if ($LASTEXITCODE -ne 0) { Fail 'The build failed.' }
}
foreach ($f in 'build\site\index.html', 'build\staff\taraba-billing.html', 'build\data\assessments.json.gz') {
    if (-not (Test-Path (Join-Path $root $f))) { Fail "$f is missing. Run without -SkipBuild." }
}

Step "Copying to ${Server}:$RemoteDir"
ssh $Server "mkdir -p $RemoteDir/deploy $RemoteDir/build"
if ($LASTEXITCODE -ne 0) { Fail 'Could not reach the VPS over SSH.' }
scp -r -q (Join-Path $root 'server') "${Server}:$RemoteDir/"
scp -q (Join-Path $root 'deploy\docker-compose.yml') (Join-Path $root 'deploy\Caddyfile') (Join-Path $root 'deploy\.env.example') "${Server}:$RemoteDir/deploy/"
scp -r -q (Join-Path $root 'build\site') (Join-Path $root 'build\staff') (Join-Path $root 'build\data') "${Server}:$RemoteDir/build/"
if ($LASTEXITCODE -ne 0) { Fail 'Copying failed.' }

Step 'Restarting containers'
ssh $Server "cd $RemoteDir/deploy && test -f .env || { echo 'deploy/.env is missing on the VPS: create it first (see README).'; exit 3; }; docker compose up -d --build"
if ($LASTEXITCODE -ne 0) { Fail 'docker compose failed on the VPS. See the message above.' }

if (-not $SkipSync) {
    Step 'Loading notices into the database'
    ssh $Server "cd $RemoteDir/deploy && docker compose exec -T app python -m app.cli sync"
    if ($LASTEXITCODE -ne 0) { Fail 'The data sync was refused. See the message above; nothing was changed.' }
}
Write-Host "`nDone." -ForegroundColor Green
