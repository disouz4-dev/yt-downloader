@echo off
chcp 65001 >nul
title Voxly YT Downloader — Instalador

echo.
echo  ╔══════════════════════════════════════╗
echo  ║   Voxly YT Downloader — Instalador  ║
echo  ╚══════════════════════════════════════╝
echo.

:: Verifica Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERRO] Python nao encontrado.
    echo  Baixe em: https://www.python.org/downloads/
    echo  Marque "Add Python to PATH" durante a instalacao.
    pause
    exit /b 1
)

echo  [1/3] Instalando dependencias...
pip install -r "%~dp0requirements.txt" --quiet
if errorlevel 1 (
    echo.
    echo  [ERRO] Falha ao instalar dependencias.
    pause
    exit /b 1
)
echo        OK

:: Define destino na Area de Trabalho
set "DEST=%USERPROFILE%\Desktop\Voxly Downloader"

echo  [2/3] Copiando arquivos para a Area de Trabalho...
if not exist "%DEST%" mkdir "%DEST%"
copy /y "%~dp0renomear_musicas.py" "%DEST%\" >nul
copy /y "%~dp0requirements.txt"    "%DEST%\" >nul
echo        OK

:: Cria o lancador .bat na pasta e na Area de Trabalho
echo  [3/3] Criando atalho de execucao...

(
    echo @echo off
    echo cd /d "%DEST%"
    echo python "%DEST%\renomear_musicas.py"
    echo if errorlevel 1 pause
) > "%DEST%\Abrir Voxly.bat"

:: Cria tambem um atalho .bat direto na raiz da Area de Trabalho
(
    echo @echo off
    echo cd /d "%DEST%"
    echo python "%DEST%\renomear_musicas.py"
    echo if errorlevel 1 pause
) > "%USERPROFILE%\Desktop\Abrir Voxly.bat"

echo        OK
echo.
echo  ════════════════════════════════════════
echo  Instalacao concluida!
echo.
echo  Pasta criada: %DEST%
echo  Atalho:       Abrir Voxly.bat na Area de Trabalho
echo  ════════════════════════════════════════
echo.
pause
