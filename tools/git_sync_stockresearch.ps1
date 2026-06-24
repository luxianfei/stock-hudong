$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".git")) {
    Write-Host "Not a git repository: $root"
    exit 1
}

# Keep the local branch aware of the remote. If the remote is unavailable,
# continue with local commit and report push failure later.
git fetch origin main 2>&1 | Write-Host

# Stage generated Q&A data, watchlist, tools, and docs. logs/ is ignored.
git add .

$status = git status --porcelain
if (-not $status) {
    Write-Host "No changes to commit."
    exit 0
}

$time = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
git commit -m "sync interactive QA $time"

# Push must be non-interactive for Task Scheduler. Configure Git Credential
# Manager, PAT, or SSH if this fails locally.
git push origin main
