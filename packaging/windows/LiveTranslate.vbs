' Starts Live Translate. The console window is only shown while the first-time
' setup runs (so its progress is visible); normal starts are silent and the app
' lives in the system tray.
Option Explicit
Dim sh, fs, app, data, ver, marker, style
Set sh = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
app = fs.GetParentFolderName(WScript.ScriptFullName)
data = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\LiveTranslate"

ver = "dev"
If fs.FileExists(app & "\VERSION") Then ver = Trim(fs.OpenTextFile(app & "\VERSION").ReadLine())
marker = data & "\venv\.installed-" & ver

If fs.FileExists(marker) Then style = 0 Else style = 1
sh.Run """" & app & "\LiveTranslate.cmd""", style, False
