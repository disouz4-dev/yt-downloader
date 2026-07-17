@echo off
chcp 65001 >nul
title Voxly YT Downloader — Atualizador

echo.
echo  Atualizando Voxly YT Downloader...
echo.

set "DEST=%USERPROFILE%\Desktop\Voxly Downloader"

if not exist "%DEST%" (
    echo  [ERRO] Programa nao instalado. Execute instalar.bat primeiro.
    pause
    exit /b 1
)

echo  Copiando nova versao...
copy /y "%~dp0renomear_musicas.py" "%DEST%\" >nul
copy /y "%~dp0requirements.txt"    "%DEST%\" >nul

echo  Atualizando dependencias...
pip install -r "%~dp0requirements.txt" --quiet --upgrade

echo.
echo  Atualizado com sucesso!
pause
