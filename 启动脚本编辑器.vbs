Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run "python """ & scriptDir & "\gui.py""", 0, False