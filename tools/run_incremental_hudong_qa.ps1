$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$log = Join-Path $logDir "hudong_qa_incremental_$stamp.log"
$python = "python"
$args = @("tools\run_incremental_hudong_qa.py")
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Start scheduled incremental Q&A collection" | Tee-Object -FilePath $log -Append
& $python @args 2>&1 | Tee-Object -FilePath $log -Append
$exitCode = $LASTEXITCODE
if ($exitCode -eq 0) {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Start git sync" | Tee-Object -FilePath $log -Append
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "git_sync_stockresearch.ps1") 2>&1 | Tee-Object -FilePath $log -Append
    $exitCode = $LASTEXITCODE
}
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Exit code: $exitCode" | Tee-Object -FilePath $log -Append
exit $exitCode

