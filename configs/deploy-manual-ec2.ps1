<#
.SYNOPSIS
  Option B: deploy from your Windows PC over SSH (no GitHub Actions).

  NEVER commit private keys (.pem, id_rsa) to git. Keep them only on your machine.

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

# Restrict key file permissions (OpenSSH on Windows warns otherwise)
icacls $KeyPath /inheritance:r /grant:r "$($env:USERNAME):(R)" 2>$null | Out-Null

# One remote shell line (avoids newline issues with Windows OpenSSH).
$remote = "set -e; cd '$RemotePath'; git fetch origin; git checkout '$Branch'; git pull origin '$Branch'; docker compose -f configs/docker-compose.yml build --pull; docker compose -f configs/docker-compose.yml up -d; docker compose -f configs/docker-compose.yml ps"

Write-Host "Connecting to ${Ec2User}@${Ec2Host} ..."
ssh -i "$KeyPath" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "${Ec2User}@${Ec2Host}" $remote
