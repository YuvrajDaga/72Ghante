' Starts the 72 Ghante server invisibly (no window). Website: http://localhost:8000
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = dir
' do nothing if it is already running
Set http = CreateObject("MSXML2.XMLHTTP")
On Error Resume Next
http.Open "GET", "http://localhost:8000/api/sim/mode", False
http.Send
If Err.Number = 0 And http.Status = 200 Then WScript.Quit
On Error GoTo 0
sh.Run """" & dir & "\.venv\Scripts\python.exe"" -m uvicorn app.main:app --port 8000", 0, False
