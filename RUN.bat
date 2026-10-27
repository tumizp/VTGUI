@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    start "" /d "%~dp0" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0main.py" %*
    exit /b 0
)
where pyw.exe >nul 2>nul
if not errorlevel 1 (
    start "" /d "%~dp0" pyw.exe -3 "%~dp0main.py" %*
    exit /b 0
)
where pythonw.exe >nul 2>nul
if not errorlevel 1 (
    start "" /d "%~dp0" pythonw.exe "%~dp0main.py" %*
    exit /b 0
)
if exist "%~dp0dist\VTGUI\VTGUI.exe" (
    start "" /d "%~dp0" "%~dp0dist\VTGUI\VTGUI.exe" %*
    exit /b 0
)
echo 未找到 Python GUI 运行环境或打包程序，请安装 Python 3.11+ 后重试。
pause
exit /b 1
