@echo off
title Ordner im Explorer einblenden
cd /d "%~dp0.."

echo Blende alle Hintergrund-Ordner wieder ein...
attrib -h "logs" 2>nul
attrib -h "models" 2>nul
attrib -h "python_core" 2>nul
attrib -h "scripts" 2>nul
attrib -h "src" 2>nul
attrib -h "App Icon.ico" 2>nul

echo Fertig! Alle Ordner sind nun wieder sichtbar.
pause
