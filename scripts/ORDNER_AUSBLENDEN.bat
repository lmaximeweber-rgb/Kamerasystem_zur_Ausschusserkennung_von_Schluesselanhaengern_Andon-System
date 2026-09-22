@echo off
title Ordner im Explorer ausblenden
cd /d "%~dp0.."

echo Blende Hintergrund-Ordner fuer Endnutzer aus...
attrib +h "logs" 2>nul
attrib +h "models" 2>nul
attrib +h "python_core" 2>nul
attrib +h "scripts" 2>nul
attrib +h "src" 2>nul
attrib +h "App Icon.ico" 2>nul

echo Fertig! Im Hauptordner ist nur noch die Startdatei sichtbar.
timeout /t 2 >nul
