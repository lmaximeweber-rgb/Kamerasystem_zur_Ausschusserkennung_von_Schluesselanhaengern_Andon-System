@echo off
title Lernfabrik Andon Assistenzsystem
if exist "%~dp0..\python_core" (
    cd /d "%~dp0.."
) else (
    cd /d "%~dp0"
)

if "%1"=="--debug" (
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
    exit
)

:: Lautloser Start ueber VBScript ohne CMD-Konsolenfenster
if exist "scripts\start_silent.vbs" (
    start "" wscript.exe "%~dp0scripts\start_silent.vbs"
    exit
) else if exist "start_silent.vbs" (
    start "" wscript.exe "%~dp0start_silent.vbs"
    exit
)

if exist "python_core\pythonw.exe" (
    if exist "src\app.py" (
        start "" "python_core\pythonw.exe" src\app.py
    ) else (
        start "" "python_core\pythonw.exe" app.py
    )
    exit
)

echo.
echo ======================================================
echo FEHLER: Keine gueltige Python-Laufzeitumgebung gefunden!
echo Der Ordner 'python_core' fehlt auf dem USB-Stick.
echo ======================================================
echo.
pause