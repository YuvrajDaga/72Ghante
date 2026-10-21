' Starts the 72 Ghante server if needed (hidden), waits until it is ready, then opens the website.
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
Function Up()
  Up = False
  On Error Resume Next
  Set http = CreateObject("MSXML2.XMLHTTP")
  http.Open "GET", "http://localhost:8000/api/sim/mode", False
  http.Send
  If Err.Number = 0 Then If http.Status = 200 Then Up = True
  On Error GoTo 0
End Function
If Not Up() Then
  sh.CurrentDirectory = dir
  sh.Run """" & dir & "\.venv\Scripts\python.exe"" -m uvicorn app.main:app --port 8000", 0, False
  For i = 1 To 40
    WScript.Sleep 1000
    If Up() Then Exit For
  Next
End If
target = "http://localhost:8000/"
If WScript.Arguments.Count > 0 Then target = WScript.Arguments(0)
sh.Run target
