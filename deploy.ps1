<#
.SYNOPSIS
    Build Taraba Central Billing and Collections and deploy it to Netlify.

.DESCRIPTION
    1. Rebuilds taraba-billing.html from the billing workbook (build.py).
    2. Stages a dist\ folder: the page as index.html, plus headers that keep it
       out of search engines and lock down what the page may load.
    3. Deploys dist\ with the Netlify CLI (installed copy, or npx netlify-cli).

    By default it makes a DRAFT deploy: a unique preview URL that is not your
    site's main address. Add -Prod to publish to the live site.

    The page contains every taxpayer's name, address and phone number. Anyone
    with the URL can open it. Restrict access with Netlify's site password
    protection (Site configuration > Access & security) before sharing it.

.EXAMPLE
    .\deploy.ps1                          # build + draft deploy (preview URL)
.EXAMPLE
    .\deploy.ps1 -Prod                    # build + production deploy
.EXAMPLE
    .\deploy.ps1 -Workbook "TIRS_2026.xlsx" -Prod
.EXAMPLE
    .\deploy.ps1 -BuildOnly               # build and stage dist\ only
.EXAMPLE
    $env:NETLIFY_AUTH_TOKEN = "..."; $env:NETLIFY_SITE_ID = "..."; .\deploy.ps1 -Prod -Yes
                                          # unattended (CI / scheduled task)
#>
[CmdletBinding()]
param(
    [switch]$Prod,                         # publish to the live site instead of a draft
    [switch]$BuildOnly,                    # stop after staging dist\
    [switch]$SkipBuild,                    # deploy the existing taraba-billing.html
    [switch]$Yes,                          # skip the production confirmation prompt
    [string]$Workbook,                     # workbook to build from (default: the one build.py uses)
    [string]$Site = $env:NETLIFY_SITE_ID,  # Netlify site ID or name
    [string]$Message                       # deploy message shown in the Netlify dashboard
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$dist = Join-Path $root 'dist'
$page = Join-Path $root 'taraba-billing.html'

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Green }
function Fail($text) { Write-Host "`nERROR: $text" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------------------
# 1. Build
# ---------------------------------------------------------------------------
if (-not $SkipBuild) {
    Step 'Building taraba-billing.html from the workbook'
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
    if (-not $python) { Fail 'Python was not found. Install Python 3 and run: pip install openpyxl' }

    $buildArgs = @((Join-Path $root 'build.py'))
    if ($Workbook) {
        if (-not (Test-Path $Workbook)) { Fail "Workbook not found: $Workbook" }
        $buildArgs += (Resolve-Path $Workbook).Path
    }
    & $python.Source @buildArgs
    if ($LASTEXITCODE -ne 0) { Fail 'The build failed. Fix the error above, then run the script again.' }
}
if (-not (Test-Path $page)) { Fail 'taraba-billing.html does not exist. Run without -SkipBuild to build it.' }

# ---------------------------------------------------------------------------
# 2. Stage dist\
# ---------------------------------------------------------------------------
Step 'Staging dist\'
if (-not (Test-Path $dist)) { New-Item -ItemType Directory -Path $dist | Out-Null }
foreach ($old in @('index.html', '_headers', 'robots.txt')) {
    $p = Join-Path $dist $old
    if (Test-Path $p) { Remove-Item $p -Force }
}
Copy-Item $page (Join-Path $dist 'index.html')

# The page is fully self-contained: inline script and styles, data: images and
# fonts, blob: downloads. Nothing else may load, and it is never indexed or framed.
$headers = @"
/*
  X-Robots-Tag: noindex, nofollow, noarchive
  X-Frame-Options: DENY
  X-Content-Type-Options: nosniff
  Referrer-Policy: no-referrer
  Permissions-Policy: camera=(), microphone=(), geolocation=()
  Content-Security-Policy: default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'
  Cache-Control: no-cache
"@
$robots = "User-agent: *`nDisallow: /`n"
# .NET writes UTF-8 without a BOM, which Netlify's _headers parser expects
[IO.File]::WriteAllText((Join-Path $dist '_headers'), ($headers -replace "`r`n", "`n"))
[IO.File]::WriteAllText((Join-Path $dist 'robots.txt'), $robots)

$sizeMb = [math]::Round((Get-Item (Join-Path $dist 'index.html')).Length / 1MB, 1)
Write-Host "    dist\index.html ($sizeMb MB), dist\_headers, dist\robots.txt"

if ($BuildOnly) {
    Write-Host "`nBuild only: dist\ is ready. Nothing was deployed." -ForegroundColor Yellow
    exit 0
}

# ---------------------------------------------------------------------------
# 3. Netlify CLI
# ---------------------------------------------------------------------------
Step 'Checking the Netlify CLI'
$useGlobal = [bool](Get-Command netlify -ErrorAction SilentlyContinue)
if (-not $useGlobal -and -not (Get-Command npx -ErrorAction SilentlyContinue)) {
    Fail 'Neither the Netlify CLI nor npx was found. Install Node.js from https://nodejs.org, or run: npm install -g netlify-cli'
}
function Invoke-Netlify {
    if ($useGlobal) { & netlify @args } else { & npx --yes netlify-cli @args }
}
Write-Host ('    Using ' + $(if ($useGlobal) { 'the installed netlify command' } else { 'npx netlify-cli (downloaded on first use)' }))

if (-not $env:NETLIFY_AUTH_TOKEN) {
    $status = (Invoke-Netlify status 2>&1 | Out-String)
    if ($status -match 'Not logged in') {
        Step 'Logging in to Netlify (a browser window will open)'
        Invoke-Netlify login
        if ($LASTEXITCODE -ne 0) { Fail 'Netlify login did not complete.' }
    }
}

# ---------------------------------------------------------------------------
# 4. Deploy
# ---------------------------------------------------------------------------
if ($Prod -and -not $Yes) {
    Write-Host "`nThis publishes all taxpayer records (names, addresses, phone numbers) to the live site." -ForegroundColor Yellow
    Write-Host 'Make sure password protection is switched on for this site in Netlify first.' -ForegroundColor Yellow
    $answer = Read-Host 'Type DEPLOY to continue'
    if ($answer -cne 'DEPLOY') { Write-Host 'Cancelled. Nothing was deployed.'; exit 0 }
}

if (-not $Message) {
    $Message = 'Taraba Central Billing and Collections ' + (Get-Date -Format 'yyyy-MM-dd HH:mm')
}
$deployArgs = @('deploy', '--dir', $dist, '--message', $Message)
if ($Site) { $deployArgs += @('--site', $Site) }
if ($Prod) { $deployArgs += '--prod' }

Step $(if ($Prod) { 'Deploying to production' } else { 'Creating a draft deploy (preview URL)' })
if (-not $Site -and -not (Test-Path (Join-Path $root '.netlify\state.json'))) {
    Write-Host '    No site linked yet: Netlify will ask you to link an existing site or create a new one.'
}
Invoke-Netlify @deployArgs
if ($LASTEXITCODE -ne 0) { Fail 'The Netlify deploy failed. See the message above.' }

Write-Host ''
if ($Prod) {
    Write-Host 'Done. The live site is updated.' -ForegroundColor Green
} else {
    Write-Host 'Done. Open the "Website draft URL" above to check it, then run .\deploy.ps1 -Prod to publish.' -ForegroundColor Green
}
