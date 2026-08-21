@echo off
setlocal EnableExtensions
title NathGPT - Mise a jour Oracle
cd /d "%~dp0"

echo.
echo ============================================
echo        MISE A JOUR NATHGPT ORACLE
echo ============================================
echo.
echo La fenetre restera ouverte meme en cas d'erreur.
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_nathgpt.ps1"
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo ============================================
    echo MISE A JOUR TERMINEE AVEC SUCCES
    echo ============================================
) else (
    echo ============================================
    echo ECHEC DE LA MISE A JOUR
    echo Code erreur : %RC%
    echo.
    echo Regarde le fichier :
    echo %~dp0mise_a_jour_nathgpt.log
    echo ============================================
)

echo.
pause
exit /b %RC%
