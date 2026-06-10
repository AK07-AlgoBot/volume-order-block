# Stop whatever is listening on the minimal AK07 API port, then start FastAPI.
# Run from repo root:  .\restart-api.ps1
param([int]$Port = 8080)

$ErrorActionPreference = "Continue"
$repoRoot = $PSScriptRoot
$serverSrc = Join-Path $repoRoot "src\server\src"

function Stop-ListenerOnPort {
    param([int]$Port)
    try {
        $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        foreach ($c in $conns) {
            $pid = $c.OwningProcess
            if ($pid -and $pid -gt 0) {
                Write-Host "Stopping process $pid on port $Port..."
                Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {
        # ignore
    }
    # Fallback: taskkill by port (older Windows)
    $lines = netstat -ano | Select-String ":$Port\s+.*LISTENING\s+(\d+)"
    foreach ($m in $lines) {
        if ($m.Matches.Count -gt 0) {
            $p = [int]$m.Matches[0].Groups[1].Value
            if ($p -gt 0) {
                Write-Host "Stopping PID $p (netstat)..."
                cmd /c "taskkill /F /PID $p >nul 2>&1"
            }
        }
    }
}

Stop-ListenerOnPort -Port $Port
Start-Sleep -Seconds 2

Write-Host "Starting AK07 minimal API on http://127.0.0.1:$Port ..."
$cmd = @"
Set-Location '$repoRoot'
`$env:PYTHONPATH = '$serverSrc'
python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
"@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($cmd))
Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-EncodedCommand", $encoded
) -WorkingDirectory $repoRoot

Write-Host "New API window opened. Health: http://127.0.0.1:$Port/api/health"
