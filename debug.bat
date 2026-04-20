@echo off
chcp 65001 >nul
title Google Photos - Cluster-Debug

echo ============================================
echo   Google Photos Cluster-Debug
echo ============================================
echo.
echo Dieses Tool dumpt fuer einen bestimmten Tag
echo ALLE Zeitquellen jeder zugehoerigen Datei in
echo eine Textdatei, damit wir sehen koennen warum
echo das Tool in einem anderen Ordner landet als
echo Google Photos anzeigt.
echo.
echo ============================================
echo.

:: Python pruefen
python --version >nul 2>&1
if errorlevel 1 (
    echo [FEHLER] Python wurde nicht gefunden!
    echo Bitte Python installieren: https://www.python.org/downloads/
    echo WICHTIG: "Add Python to PATH" ankreuzen!
    pause
    exit /b 1
)

:: Entpackte Daten pruefen
if not exist "temp_analyze\" (
    echo [FEHLER] Ordner "temp_analyze\" existiert nicht.
    echo Bitte zuerst start.bat laufen lassen, damit
    echo die ZIPs entpackt werden.
    pause
    exit /b 1
)

:: Abhaengigkeiten sicherstellen
pip install -r requirements.txt --quiet

:: Datum abfragen
echo.
set /p TARGET_DATE="Welches Datum debuggen? (Format YYYY-MM-DD, z.B. 2023-07-30): "
if "%TARGET_DATE%"=="" (
    echo [FEHLER] Kein Datum eingegeben.
    pause
    exit /b 1
)

echo.
echo Starte Debug-Lauf fuer %TARGET_DATE% ...
echo.

python debug_cluster.py --temp temp_analyze --date %TARGET_DATE% --out debug_%TARGET_DATE%.txt
if errorlevel 1 (
    echo.
    echo [FEHLER] Debug-Lauf fehlgeschlagen.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Fertig!
echo ============================================
echo.
echo Dump-Datei: %CD%\debug_%TARGET_DATE%.txt
echo.
echo Inhalt im Claude-Chat einfuegen (2-3 Dateien
echo reichen meistens), damit der Zeitzonen-Konflikt
echo analysiert werden kann.
echo.

:: Datei direkt im Editor oeffnen
if exist "debug_%TARGET_DATE%.txt" (
    echo Oeffne Datei im Editor ...
    start "" notepad "debug_%TARGET_DATE%.txt"
)

echo.
pause
