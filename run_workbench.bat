@echo off
rem Beacon launcher.
rem Prefers the project .venv created by setup, then any Python on PATH.
rem Uses pythonw (no console window) when available. Run setup.bat first if
rem you have not installed the tools yet.
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0research_workbench.py"
  goto :eof
)
if exist "%~dp0.venv\Scripts\python.exe" (
  start "" "%~dp0.venv\Scripts\python.exe" "%~dp0research_workbench.py"
  goto :eof
)
where pythonw >nul 2>nul && (start "" pythonw "%~dp0research_workbench.py" & goto :eof)
where python  >nul 2>nul && (start "" python  "%~dp0research_workbench.py" & goto :eof)
echo Python was not found. Run setup.bat first, or install Python 3.9+ from https://python.org
pause
