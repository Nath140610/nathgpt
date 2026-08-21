$ErrorActionPreference = "Stop"

$SshKey = "C:\Users\natha\Downloads\ssh-key-2026-08-19 (1).key"
$Vm = "ubuntu@158.178.213.168"
$RemoteDir = "/home/ubuntu/NathGPT_V11_Oracle"
$RemoteUid = "1001"
$LocalScript = Join-Path $PSScriptRoot "nathgpt_v11_oracle.py"
$LogFile = Join-Path $PSScriptRoot "mise_a_jour_nathgpt.log"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host $Text
}

function Run-Native {
    param(
        [Parameter(Mandatory=$true)][string]$File,
        [Parameter(Mandatory=$true)][string[]]$Arguments
    )

    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$File a retourne le code $LASTEXITCODE."
    }
}

try {
    Start-Transcript -Path $LogFile -Append | Out-Null

    Write-Host "NathGPT - mise a jour Oracle"
    Write-Host "VM : $Vm"

    if (-not (Test-Path -LiteralPath $SshKey)) {
        throw "Cle SSH introuvable : $SshKey"
    }

    if (-not (Test-Path -LiteralPath $LocalScript)) {
        throw "Fichier nathgpt_v11_oracle.py introuvable dans : $PSScriptRoot"
    }

    if (-not (Get-Command ssh.exe -ErrorAction SilentlyContinue)) {
        throw "ssh.exe est introuvable. Installe le client OpenSSH de Windows."
    }

    if (-not (Get-Command scp.exe -ErrorAction SilentlyContinue)) {
        throw "scp.exe est introuvable. Installe le client OpenSSH de Windows."
    }

    Write-Step "[1/6] Test de la connexion SSH..."
    Run-Native "ssh.exe" @(
        "-o", "ConnectTimeout=12",
        "-i", $SshKey,
        $Vm,
        "echo connexion_ok"
    )

    Write-Step "[2/6] Envoi du nouveau code..."
    Run-Native "scp.exe" @(
        "-i", $SshKey,
        $LocalScript,
        "${Vm}:/home/ubuntu/nathgpt_v11_oracle.py.new"
    )

    Write-Step "[3/6] Verification de la syntaxe Python sur la VM..."
    Run-Native "ssh.exe" @(
        "-i", $SshKey,
        $Vm,
        "$RemoteDir/.venv/bin/python -m py_compile /home/ubuntu/nathgpt_v11_oracle.py.new"
    )

    Write-Step "[4/6] Sauvegarde de l'ancienne version et installation..."
    Run-Native "ssh.exe" @(
        "-i", $SshKey,
        $Vm,
        "cp '$RemoteDir/nathgpt_v11_oracle.py' '$RemoteDir/nathgpt_v11_oracle.py.backup' && mv /home/ubuntu/nathgpt_v11_oracle.py.new '$RemoteDir/nathgpt_v11_oracle.py' && chmod 644 '$RemoteDir/nathgpt_v11_oracle.py'"
    )

    Write-Step "[5/6] Redemarrage de NathGPT..."
    $UserEnv = "XDG_RUNTIME_DIR=/run/user/$RemoteUid DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$RemoteUid/bus"
    Run-Native "ssh.exe" @(
        "-i", $SshKey,
        $Vm,
        "$UserEnv systemctl --user restart nathgpt"
    )

    Start-Sleep -Seconds 4

    Write-Step "[6/6] Verification du service..."
    Run-Native "ssh.exe" @(
        "-i", $SshKey,
        $Vm,
        "$UserEnv systemctl --user status nathgpt --no-pager -l"
    )

    Write-Host ""
    Write-Host "Mise a jour terminee."
    Write-Host "Le .env, le token Discord et le profil Chromium ont ete conserves."
    Write-Host "Ancienne version sauvegardee dans :"
    Write-Host "$RemoteDir/nathgpt_v11_oracle.py.backup"

    Stop-Transcript | Out-Null
    exit 0
}
catch {
    Write-Host ""
    Write-Host "ERREUR : $($_.Exception.Message)"
    Write-Host ""
    Write-Host "Le detail est aussi enregistre dans :"
    Write-Host $LogFile

    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}
