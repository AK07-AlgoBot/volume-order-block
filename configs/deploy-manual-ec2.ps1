<#
.SYNOPSIS
  Deploy AK07 to EC2 from Windows over SSH.

.EXAMPLE
  .\configs\deploy-manual-ec2.ps1 -Ec2Host "203.0.113.10" -KeyPath "C:\Users\pavan\arun\id_rsa"

.EXAMPLE
  .\configs\deploy-manual-ec2.ps1 -Ec2Host "ak07.in" -Ec2User "ubuntu" -RemotePath "/home/ubuntu/volume-order-block"
#>
param(
    [Parameter(Mandatory = $true)][string]$Ec2Host,
    [string]$Ec2User = "ubuntu",
    [string]$KeyPath = "C:\Users\pavan\arun\id_rsa",
    [string]$RemotePath = "/home/ubuntu/volume-order-block",
    [string]$Branch = "AK07"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $KeyPath)) {
    throw "SSH private key not found: $KeyPath"
}

# Restrict key file permissions (OpenSSH on Windows warns otherwise).
icacls $KeyPath /inheritance:r /grant:r "$($env:USERNAME):(R)" 2>$null | Out-Null

$remote = @"
set -e
cd '$RemotePath'
git fetch origin
git checkout '$Branch'
git pull --ff-only origin '$Branch'
chmod +x configs/deploy-ec2.sh
DEPLOY_BRANCH='$Branch' configs/deploy-ec2.sh '$RemotePath'
"@

Write-Host "Deploying AK07 to ${Ec2User}@${Ec2Host}:${RemotePath} (branch ${Branch}) ..."
ssh -i "$KeyPath" `
    -o IdentitiesOnly=yes `
    -o BatchMode=yes `
    -o StrictHostKeyChecking=accept-new `
    "${Ec2User}@${Ec2Host}" `
    $remote
