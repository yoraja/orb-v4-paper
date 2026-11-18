' run-hidden-wait.vbs - run a PowerShell script with no console window, WAIT for it,
' and return its exit code. Used by 'ORB v4 PAPER Primary' so the 1-minute tick does not
' pop a console on the desktop (closing it would kill a scan), while Task Scheduler still
' sees the real run time (IgnoreNew, 12-minute limit) and the real exit code.
' usage: wscript.exe //B //Nologo run-hidden-wait.vbs "C:\path\script.ps1" [args...]
Dim sh, i, args, rc
Set sh = CreateObject("WScript.Shell")
args = ""
For i = 1 To WScript.Arguments.Count - 1
  args = args & " " & WScript.Arguments(i)
Next
rc = sh.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & WScript.Arguments(0) & """" & args, 0, True)
WScript.Quit rc
