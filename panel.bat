@echo off
rem 启动交易台窗口（独立窗口，不是网页）。桌面快捷方式指向 scripts\panel.pyw，同一处。
rem 界面代码只有一份：web/ 由服务端挂在 /desk，窗口把它装进独立窗口。
rem 旧面板（实盘四页签）仍在：scripts\panel.pyw legacy
set "ROOT=%~dp0"
start "" "%ROOT%.venv\Scripts\pythonw.exe" "%ROOT%scripts\panel.pyw"
