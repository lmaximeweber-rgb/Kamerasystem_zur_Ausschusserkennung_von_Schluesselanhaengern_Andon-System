@echo off
title Lernfabrik Andon Assistenzsystem (Debug-Modus)
cd /d "%~dp0.."

echo ======================================================
echo Starte Lernfabrik KI-Pruefstation im Debug-Modus...
echo ======================================================

if exist "python_core\python.exe" (
    if exist "src\app.py" (
        "python_core\python.exe" src\app.py
    ) else (
        "python_core\python.exe" app.py
    )
) else if exist "venv\Scripts\python.exe" (
    if exist "src\app.py" (
        "venv\Scripts\python.exe" src\app.py
    ) else (
        "venv\Scripts\python.exe" app.py
    )
) else (
    echo FEHLER: Keine gueltige Python-Laufzeitumgebung gefunden!
)

pause
