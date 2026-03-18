param(
    [switch]$Force,
    [switch]$UsePreviewUI = $true
)

$ErrorActionPreference = "Stop"

function Test-HttpEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Url
    )

    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 400
    }
    catch {
        return $false
    }
}

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
    param(
        [Parameter(Mandatory = $true)][string]$Pattern
    )

    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'powershell.exe'"
        foreach ($proc in $procs) {
            if ($proc.CommandLine -and $proc.CommandLine -like "*$Pattern*") {
                return $true
            }
        }
        return $false
    }
    catch {
        return $false
    }
}

$repoRoot = $PSScriptRoot
$uiPath = Join-Path $repoRoot "dashboard-ui"

if (-not (Test-Path $uiPath)) {
    throw "dashboard-ui folder not found at $uiPath"
}

# Resolve Node/npm (system first, fallback to local portable Node)
$nodeCmd = "node"
$npmCmd = "npm"
$nodeVersionOk = $false
$portableNodeDir = $null

try {
    node -v | Out-Null
    npm -v | Out-Null
    $nodeVersionOk = $true
}
catch {
    $nodeDir = Get-ChildItem (Join-Path $repoRoot ".tools\node") -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "node-v*-win-x64" } |
        Sort-Object Name -Descending |
        Select-Object -First 1

    if ($null -eq $nodeDir) {
        throw "Node.js not found. Install Node.js or keep portable Node under .tools\node."
    }

    $nodeCmd = Join-Path $nodeDir.FullName "node.exe"
    $npmCmd = Join-Path $nodeDir.FullName "npm.cmd"
    $portableNodeDir = $nodeDir.FullName

    & $nodeCmd -v | Out-Null
    & $npmCmd -v | Out-Null
    $nodeVersionOk = $true
}

if (-not $nodeVersionOk) {
    throw "Node.js/npm unavailable."
}

# If using portable Node, ensure npm script subprocesses can resolve node.exe.
if ($portableNodeDir) {
    $env:Path = "$portableNodeDir;$env:Path"
}

# Ensure UI dependencies are installed
if (-not (Test-Path (Join-Path $uiPath "node_modules"))) {
    Write-Host "Installing dashboard UI dependencies..."
    if ($npmCmd -eq "npm") {
        & npm install --prefix $uiPath
    }
    else {
        & $npmCmd install --prefix $uiPath
    }
}

$apiUp = Test-HttpEndpoint -Url "http://127.0.0.1:8000/api/dashboard/initial"
$uiUp = Test-HttpEndpoint -Url "http://127.0.0.1:5173"

if ($apiUp -and -not $Force) {
    Write-Host "API already running on http://localhost:8000 (use -Force to start another)."
}
else {
    Start-ManagedProcess -Name "Dashboard API" -WorkingDirectory $repoRoot -Command @"
Set-Location '$repoRoot'
python -m uvicorn dashboard_api:app --host 0.0.0.0 --port 8000 --reload
"@
}

if ($uiUp -and -not $Force) {
    Write-Host "UI already running on http://localhost:5173 (use -Force to start another)."
}
else {
    if ($UsePreviewUI) {
        if ($npmCmd -eq "npm") {
            & npm run build --prefix $uiPath
            $uiCommand = @"
Set-Location '$uiPath'
npm run preview -- --host 0.0.0.0 --port 5173
"@
        }
        else {
            $escapedNodeDir = Split-Path $npmCmd -Parent
            & $npmCmd run build --prefix $uiPath
            $uiCommand = @"
Set-Location '$uiPath'
\$env:Path = '$escapedNodeDir;' + \$env:Path
& '$npmCmd' run preview -- --host 0.0.0.0 --port 5173
"@
        }
    }
    else {
        if ($npmCmd -eq "npm") {
            $uiCommand = @"
Set-Location '$uiPath'
npm run dev -- --host 0.0.0.0 --port 5173
"@
        }
        else {
            $escapedNodeDir = Split-Path $npmCmd -Parent
            $uiCommand = @"
Set-Location '$uiPath'
\$env:Path = '$escapedNodeDir;' + \$env:Path
& '$npmCmd' run dev -- --host 0.0.0.0 --port 5173
"@
        }
    }

    Start-ManagedProcess -Name "Dashboard UI" -WorkingDirectory $uiPath -Command $uiCommand
}

$botRunning = Test-CommandLineProcess -Pattern "trading_bot.py"
if ($botRunning -and -not $Force) {
    Write-Host "Trading bot appears to be already running (use -Force to start another)."
}
else {
    Start-ManagedProcess -Name "Trading Bot" -WorkingDirectory $repoRoot -Command @"
Set-Location '$repoRoot'
python trading_bot.py
"@
}

Write-Host ""
Write-Host "All launch commands sent."
Write-Host "Dashboard UI: http://localhost:5173"
Write-Host "Dashboard API: http://localhost:8000"
Write-Host ""
Write-Host "Tip: Use .\run_all.ps1 -Force to start new API/UI instances even if ports are already active."
Write-Host "Tip: Use .\run_all.ps1 -UsePreviewUI:\$false for Vite dev server."
