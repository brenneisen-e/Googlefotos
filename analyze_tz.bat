@echo off
chcp 65001 >nul
title Google Photos - TZ-Kalibrierung

echo ============================================
echo   Google Photos TZ-Kalibrierung (10 Dateien)
echo ============================================
echo.
echo Dieser Lauf pickt 10 strategisch ausgewaehlte Dateien
echo aus temp_analyze\ und schreibt eine Diagnose-Datei.
echo.
echo Du fuellst bei jeder Datei das von Google Photos
echo angezeigte Datum ein und schickst den INHALT an Claude.
echo Claude wertet aus und nennt dir die korrekte Timezone.
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
    echo Bitte zuerst start.bat laufen lassen (entpackt die ZIPs).
    pause
    exit /b 1
)

pip install -r requirements.txt --quiet

echo.
echo [1/2] Waehle 10 Kalibrierungs-Dateien aus ...
python analyze_tz.py --temp temp_analyze --out tz_analysis.txt
if errorlevel 1 (
    echo [FEHLER] Auswahl fehlgeschlagen.
    pause
    exit /b 1
)

echo.
echo [2/2] tz_analysis.txt wird im Editor geoeffnet.
echo.
echo       ANLEITUNG:
echo       1) Fuer jede der 10 FILENAME-Zeilen: Dateinamen kopieren
echo       2) In photos.google.com oben in die Suche einfuegen
echo       3) Angezeigtes Datum in 'GOOGLE_ZEIGT:' eintragen
echo          (Format: 2023-07-29 oder 29.07.2023)
echo       4) Datei speichern
echo       5) KOMPLETTEN INHALT der Datei kopieren und in Claude
echo          einfuegen - Claude nennt dir die Timezone.
echo.

:: Notepad blockierend oeffnen
start /wait "" notepad "tz_analysis.txt"

echo.
echo ============================================
echo   Fertig!
echo ============================================
echo.
echo Pfad: %CD%\tz_analysis.txt
echo.
echo Jetzt den kompletten Inhalt der Datei an Claude senden.
echo Claude wertet aus und sagt dir welche TZ zu nutzen ist.
echo.

pause
