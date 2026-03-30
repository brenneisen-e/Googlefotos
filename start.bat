@echo off
chcp 65001 >nul
title Google Photos Metadaten-Analyse

echo ============================================
echo   Google Photos Metadaten-Analyse
echo ============================================
echo.

:: Python pruefen
python --version >nul 2>&1
if errorlevel 1 (
    echo [FEHLER] Python wurde nicht gefunden!
    echo.
    echo Bitte Python installieren von: https://www.python.org/downloads/
    echo WICHTIG: Beim Installieren "Add Python to PATH" ankreuzen!
    echo.
    pause
    exit /b 1
)

echo [1/3] Python gefunden.
echo [2/3] Abhaengigkeiten werden installiert...
echo.
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [FEHLER] Installation fehlgeschlagen.
    pause
    exit /b 1
)

echo.
echo [3/3] Analyse wird gestartet...
echo.
python analyze.py

echo.
echo ============================================
echo   Fertig! Die Datei metadaten_report.xlsx
echo   liegt jetzt in diesem Ordner.
echo ============================================
echo.
pause
