@echo off
chcp 65001 >nul
title Google Photos - TZ-Kalibrierung

echo ============================================
echo   Google Photos TZ-Kalibrierung (10 Dateien)
echo ============================================
echo.
echo Diese einmalige Kalibrierung findet heraus, in welcher
echo Zeitzone Google Photos die Datumslabels anzeigt. Ohne
echo diesen Schritt landen Cluster-Ordner evtl. am falschen Tag.
echo.
echo Ablauf:
echo   1. Tool wählt 10 strategisch ausgewählte Dateien aus
echo   2. Datei tz_analysis.txt wird geöffnet
echo   3. Du suchst jeden Dateinamen in Google Photos und
echo      trägst das ANGEZEIGTE Datum bei GOOGLE_ZEIGT: ein
echo   4. Datei speichern + schließen
echo   5. Tool wertet automatisch aus und speichert die TZ
echo      in 'google_tz.txt' — repair.bat liest sie dann.
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
echo [1/3] Waehle 10 Kalibrierungs-Dateien aus ...
python analyze_tz.py --temp temp_analyze --out tz_analysis.txt
if errorlevel 1 (
    echo [FEHLER] Auswahl fehlgeschlagen.
    pause
    exit /b 1
)

echo.
echo [2/3] tz_analysis.txt wird im Editor geoeffnet.
echo.
echo       ANLEITUNG:
echo       - Jede der 10 FILENAME-Zeilen kopieren
echo       - In photos.google.com oben die Suchleiste nutzen
echo       - Angezeigtes Datum in GOOGLE_ZEIGT: eintragen
echo         (Format: 2023-07-29 oder 29.07.2023)
echo       - Datei speichern + Editor SCHLIESSEN
echo         (das Tool wartet auf Schliessen)
echo.

:: Notepad blockierend oeffnen — das Tool wartet bis geschlossen
start /wait "" notepad "tz_analysis.txt"

echo.
echo [3/3] Antworten auswerten ...
python analyze_tz.py --verify tz_analysis.txt --save-tz google_tz.txt

if exist "google_tz.txt" (
    set /p DETECTED=<google_tz.txt
    echo.
    echo ============================================
    echo   Kalibrierung erfolgreich!
    echo ============================================
    echo.
    echo Gefundene TZ wurde in google_tz.txt gespeichert.
    echo.
    echo Jetzt repair.bat starten - die TZ wird automatisch
    echo uebernommen und die Cluster-Ordner stimmen dann
    echo mit dem Datum ueberein, das Google Photos anzeigt.
    echo.
) else (
    echo.
    echo ============================================
    echo   Kalibrierung noch nicht eindeutig
    echo ============================================
    echo.
    echo Meist hilft: Eintraege in tz_analysis.txt pruefen
    echo ^(mindestens einen, wo NY und LA unterschiedliche
    echo  Tage liefern^), dann diese .bat nochmal laufen lassen.
    echo.
)

pause
