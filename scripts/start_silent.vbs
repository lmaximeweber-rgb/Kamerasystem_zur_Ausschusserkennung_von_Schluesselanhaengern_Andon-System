Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

' Projekt-Hauptverzeichnis ermitteln
Dim projectDir
If fso.FolderExists(scriptDir & "\python_core") Then
    projectDir = scriptDir
Else
    projectDir = fso.GetParentFolderName(scriptDir)
End If

Set wshShell = CreateObject("WScript.Shell")
wshShell.CurrentDirectory = projectDir

' Shortcut-Icon Pfad aktualisieren, falls sich der USB-Laufwerksbuchstabe geaendert hat
Dim lnkPath, iconPath
lnkPath = projectDir & "\Andon Prüfstation.lnk"
iconPath = projectDir & "\App Icon.ico"
If fso.FileExists(lnkPath) And fso.FileExists(iconPath) Then
    On Error Resume Next
    Dim lnk
    Set lnk = wshShell.CreateShortcut(lnkPath)
    If InStr(1, lnk.IconLocation, iconPath, vbTextCompare) = 0 Then
        lnk.TargetPath = "%SystemRoot%\System32\wscript.exe"
        lnk.Arguments = "scripts\start_silent.vbs"
        lnk.IconLocation = iconPath & ",0"
        lnk.Description = "Lernfabrik Andon Assistenzsystem"
        lnk.Save
    End If
    On Error GoTo 0
End If

Dim pythonExe
If fso.FileExists(projectDir & "\python_core\pythonw.exe") Then
    pythonExe = """" & projectDir & "\python_core\pythonw.exe"""
ElseIf fso.FileExists(projectDir & "\venv\Scripts\pythonw.exe") Then
    pythonExe = """" & projectDir & "\venv\Scripts\pythonw.exe"""
Else
    MsgBox "FEHLER: Keine gueltige Python-Laufzeitumgebung ('python_core') auf dem USB-Stick gefunden!", 16, "Andon Pruefstation"
    WScript.Quit 1
End If

Dim appPath
If fso.FileExists(projectDir & "\src\app.py") Then
    appPath = """" & projectDir & "\src\app.py"""
Else
    appPath = """" & projectDir & "\app.py"""
End If

' Fensterstil 1 (SW_SHOWNORMAL): pythonw.exe besitzt kein Konsolenfenster,
' aber das Qt-GUI-Fenster wird normal sichtbar dargestellt.
wshShell.Run pythonExe & " " & appPath, 1, False
