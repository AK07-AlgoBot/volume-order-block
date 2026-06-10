param(
    [switch]$Force,
    [switch]$Mock,
    [switch]$EngineOnly,
    [switch]$DashboardOnly,
    [switch]$McpOnly,
    [switch]$ApiOnly,
    [int]$DashboardPort = 8501,
    [int]$McpPort = 8765,
    [int]$ApiPort = 8080
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$serverSrc = Join-Path $repoRoot "src\server\src"

function Start-ManagedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$Command
    )
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Command))
    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-ExecutionPolicy", "Bypass",
        "-EncodedCommand", $encoded
    ) -WorkingDirectory $WorkingDirectory | Out-Null
    Write-Host "Started $Name"
}

function Test-CommandLineProcess {
    param([Parameter(Mandatory = $true)][string]$Pattern)
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'powershell.exe'"
        foreach ($proc in $procs) {
            if ($proc.CommandLine -and $proc.CommandLine -like "*$Pattern*") { return $true }
        }
    } catch {}
    return $false
}

function Start-IfNeeded {
    param(
        [string]$Name,
        [string]$Pattern,
        [string]$Command
    )
    if ((Test-CommandLineProcess -Pattern $Pattern) -and -not $Force) {
        Write-Host "$Name appears to be already running (use -Force to start another)."
        return
    }
    Start-ManagedProcess -Name $Name -WorkingDirectory $repoRoot -Command $Command
}

if ($Mock) {
    $envLine = "`$env:AK07_MOCK = '1'"
    Write-Host "AK07 mock mode enabled (fakeredis + simulated market data)."
} else {
    $envLine = "`$env:AK07_MOCK = `$null"
}

$common = @"
Set-Location '$repoRoot'
`$env:PYTHONPATH = '$serverSrc'
$envLine
"@

if (-not $DashboardOnly -and -not $McpOnly -and -not $ApiOnly) {
    Start-IfNeeded -Name "AK07 Engine" -Pattern "upstox_engine.py" -Command @"
$common
python src\server\src\app\services\upstox_engine.py
"@
}

if (-not $EngineOnly -and -not $DashboardOnly -and -not $ApiOnly) {
    Start-IfNeeded -Name "AK07 MCP Server" -Pattern "mcp_server.py" -Command @"
$common
python src\server\src\mcp_server.py --host 127.0.0.1 --port $McpPort
"@
}

if (-not $EngineOnly -and -not $McpOnly -and -not $ApiOnly) {
    Start-IfNeeded -Name "AK07 Streamlit Cockpit" -Pattern "dashboard.py" -Command @"
$common
python -m streamlit run src\server\src\app\ui\dashboard.py --server.port $DashboardPort
"@
}

if (-not $EngineOnly -and -not $McpOnly -and -not $DashboardOnly) {
    Start-IfNeeded -Name "AK07 Minimal API" -Pattern "uvicorn app.main:app" -Command @"
$common
python -m uvicorn app.main:app --host 127.0.0.1 --port $ApiPort
"@
}

Write-Host ""
Write-Host "AK07 launch commands sent."
Write-Host "Cockpit : http://localhost:$DashboardPort"
Write-Host "MCP     : http://127.0.0.1:$McpPort/mcp"
Write-Host "API     : http://127.0.0.1:$ApiPort/api/health"
Write-Host ""
Write-Host "Useful modes:"
Write-Host "  .\start.ps1 -Mock              # visual cockpit with fake data, no Redis/broker"
Write-Host "  .\start.ps1 -EngineOnly        # engine only"
Write-Host "  .\start.ps1 -DashboardOnly -Mock"
