' Starts Live Translate. Normal starts are silent and the app lives in the system tray. When the
' software it runs on still has to be installed, LiveTranslateSetup.ps1 shows a window for that
' and starts the app itself; if it can't (e.g. PowerShell is blocked here), LiveTranslate.cmd does
' the same work in a console window, so its progress is still visible.
Option Explicit
Dim sh, fs, app, data, location, ver, marker, style, args, arg, code
Set sh = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
app = fs.GetParentFolderName(WScript.ScriptFullName)
data = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\LiveTranslate"

' The folder picked in the setup window (UTF-16, so any folder name survives).
location = data & "\data-location.txt"
If fs.FileExists(location) Then
    data = Trim(fs.OpenTextFile(location, 1, False, -1).ReadLine())
    sh.Environment("Process")("LIVETRANSLATE_DATA") = data
End If

ver = "dev"
If fs.FileExists(app & "\VERSION") Then ver = Trim(fs.OpenTextFile(app & "\VERSION").ReadLine())
marker = data & "\venv\.installed-" & ver

args = ""
For Each arg In WScript.Arguments
    args = args & " " & arg
Next

If fs.FileExists(marker) Then
    style = 0
Else
    code = sh.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & app & "\LiveTranslateSetup.ps1"" -AppArgs """ & Trim(args) & """", 0, True)
    If code = 0 Or code = 10 Then WScript.Quit
    style = 1
End If
sh.Run """" & app & "\LiveTranslate.cmd""" & args, style, False
