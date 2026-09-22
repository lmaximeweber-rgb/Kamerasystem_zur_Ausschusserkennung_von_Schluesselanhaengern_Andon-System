@echo off
title Andon Pruefstation - Verknuepfung erstellen
cd /d "%~dp0.."

echo Erstelle 'Andon Pruefstation.lnk' mit App-Icon...

powershell -NoProfile -ExecutionPolicy Bypass -Command "$WshShell = New-Object -ComObject WScript.Shell; $Shortcut = $WshShell.CreateShortcut('Andon Prüfstation.lnk'); $Shortcut.TargetPath = '%SystemRoot%\System32\wscript.exe'; $Shortcut.Arguments = 'scripts\start_silent.vbs'; $Shortcut.IconLocation = (Resolve-Path 'App Icon.ico').Path + ',0'; $Shortcut.Description = 'Lernfabrik Andon Assistenzsystem'; $Shortcut.Save()"

echo Blende Hintergrundordner aus...
attrib +h "logs" 2>nul
attrib +h "models" 2>nul
attrib +h "python_core" 2>nul
attrib +h "scripts" 2>nul
attrib +h "src" 2>nul
attrib +h "App Icon.ico" 2>nul

echo Fertig! Im Hauptordner ist nun ausschliesslich die Startdatei mit App-Icon sichtbar.
timeout /t 2 >nul
