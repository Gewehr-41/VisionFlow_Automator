' 启动旧版 Tkinter 界面（已退役，移入 legacy/）
'
' 重要：gui.py 依赖【工作目录】而不是自身位置来解析 import
'       （它 import config / main / tasks / nodes / core.*），
'       因此必须把工作目录设为项目根目录，再以相对路径运行 legacy\gui.py。
'
' 注意：这是已退役的旧界面，与 PySide6 版共享同一批 JSON 数据文件，
'       运行并保存可能覆盖新界面的数据。详见 legacy\README.md。
'       日常使用请运行 启动新界面.bat。

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = scriptDir

sh.Run "python """ & scriptDir & "\legacy\gui.py""", 0, False
