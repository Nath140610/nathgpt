@echo off
title NathGPT - Ecran VM Oracle

REM === Configuration ===
set "SSH_KEY=C:\Users\natha\Downloads\ssh-key-2026-08-19 (1).key"
set "VM_USER=ubuntu"
set "VM_IP=158.178.213.168"
set "LOCAL_PORT=6080"

REM === Ouvre le tunnel SSH en arriere-plan dans une fenetre reduite ===
start "NathGPT VM Tunnel" /min powershell.exe -NoProfile -WindowStyle Minimized -Command ^
"ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -i '%SSH_KEY%' -L %LOCAL_PORT%:127.0.0.1:%LOCAL_PORT% %VM_USER%@%VM_IP%"

REM === Attend que le tunnel soit initialise ===
timeout /t 3 /nobreak >nul

REM === Ouvre noVNC dans le navigateur par defaut ===
start "" "http://127.0.0.1:6080/vnc.html?autoconnect=true&resize=scale"

exit
