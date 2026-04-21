@echo off
chcp 65001 >nul
title Google Photos Repair (Cluster-Modus)

echo ============================================
echo   Google Photos Repair (Cluster-Modus)
echo ============================================
echo.
echo Dieser Lauf baut Ordner, die nach dem Datum
echo benannt sind, das Google Photos aktuell anzeigt.
echo In jedem Ordner sind ALLE Dateien dieses Tages,
echo alle mit korrigiertem EXIF/Datum.
echo.
echo Es werden NUR Tage kopiert, an denen mehr als
echo 10 Dateien eine Abweichung haben. Kleine Cluster
echo und Dateien ohne JSON werden uebersprungen.
echo.
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
echo [1/5] Python gefunden.

:: Entpackte Daten aus dem Analyse-Lauf pruefen
if not exist "temp_analyze\" (
    echo.
    echo [FEHLER] Der Ordner "temp_analyze" existiert nicht.
    echo.
    echo Bitte zuerst die Analyse laufen lassen:
    echo    Doppelklick auf start.bat
    echo.
    pause
    exit /b 1
)
echo [2/5] Entpackte Daten gefunden in temp_analyze\

:: Abhaengigkeiten sicherstellen
echo [3/5] Abhaengigkeiten pruefen...
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [FEHLER] Installation der Abhaengigkeiten fehlgeschlagen.
    pause
    exit /b 1
)

:: ExifTool pruefen (Warnung, kein harter Abbruch)
python -c "from modules.metadata import find_exiftool; import sys; sys.exit(0 if find_exiftool() else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo [WARNUNG] ExifTool wurde nicht gefunden!
    echo.
    echo Fuer HEIC-Fotos und Videos wird ExifTool benoetigt.
    echo Falls du exiftool-13.55_64\ neben dieser Datei liegen hast,
    echo benenne 'exiftool^(-k^).exe' in 'exiftool.exe' um.
    echo Der Ordner 'exiftool_files' muss dabei daneben bleiben.
    echo.
    echo Der Repair-Lauf startet trotzdem in 15 Sekunden...
    echo Druecke STRG+C zum Abbrechen.
    timeout /t 15 >nul
) else (
    echo [4/5] ExifTool gefunden und einsatzbereit.
)

:: Alten Output vorhanden?
if exist "output\" (
    echo.
    echo [!] Ein Ordner "output\" existiert bereits.
    echo     Ein frischer Lauf ueberschreibt nichts, aber die alte
    echo     Datenbank photos.db kann kollidieren.
    echo.
    set /p CONFIRM="Alten output-Ordner und photos.db loeschen? (j/n): "
)
if /i "%CONFIRM%"=="j" (
    if exist "output\" rmdir /s /q output
    if exist "photos.db" del photos.db
    echo     Geloescht.
    echo.
)

echo [5/5] Repair-Lauf wird gestartet...
echo.

:: Google-Photos-Anzeige-TZ aus google_tz.txt lesen (wenn vorhanden).
:: `for /f` strippt automatisch CR, BOM und leere Zeilen — set /p dagegen
:: liefert ggf. CR-Reste mit, was dann "--google-tz America/Los_Angeles\r"
:: an Python uebergibt und eine ZoneInfoNotFoundError ausloest.
setlocal enabledelayedexpansion
set GOOGLE_TZ_ARG=
set DETECTED_TZ=
if exist "google_tz.txt" (
    for /f "usebackq tokens=* delims=" %%A in ("google_tz.txt") do (
        if not "%%A"=="" set DETECTED_TZ=%%A
    )
    if not "!DETECTED_TZ!"=="" (
        set GOOGLE_TZ_ARG=--google-tz !DETECTED_TZ!
        echo       Nutze kalibrierte TZ: !DETECTED_TZ!
        echo.
    )
) else (
    echo [HINWEIS] google_tz.txt nicht gefunden - Cluster-Ordner
    echo           koennten am falschen Tag landen. Fuer Sicherheit
    echo           vor dem Repair einmal analyze_tz.bat ausfuehren.
    echo.
)

python repair.py --skip-extraction --temp temp_analyze --cluster-by-json-date --min-cluster-mismatches 10 --skip-no-json-date !GOOGLE_TZ_ARG!
endlocal

echo.
echo ============================================
echo   Fertig!
echo ============================================
echo.
echo Die reparierten Dateien liegen in:
echo.
echo    %CD%\output\
echo.
echo Jeder Unterordner ist nach dem Datum benannt,
echo das Google Photos AKTUELL anzeigt. Beispiel:
echo.
echo    output\2015-05-29\   ^(alle Dateien von diesem Tag^)
echo    output\2023-08-07\   ^(alle Dateien von diesem Tag^)
echo.
echo Nur Tage mit ^>10 Abweichungen sind enthalten.
echo Dateien ohne JSON-Sidecar werden uebersprungen.
echo.
echo Workflow pro Ordner:
echo   1. Google Photos oeffnen ^(photos.google.com^)
echo   2. Zum selben Datum scrollen, alle Fotos markieren
echo   3. In Google Photos loeschen
echo   4. Den entsprechenden output-Ordner per
echo      Drag and Drop ins Google-Photos-Fenster ziehen
echo.
echo Google liest das korrigierte EXIF und legt die
echo Dateien bei ihrem ECHTEN Datum ab.
echo.
echo Details zu jeder Datei stehen in:
echo    %CD%\output\repair_log.csv
echo ============================================
echo.
pause
