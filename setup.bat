@echo off
rem Beacon - first-time setup.
rem Double-click this file. It runs setup.ps1, which checks for Python, a
rem virtual environment, git, LaTeX (TinyTeX), and the optional AI tools,
rem and offers to install whatever is missing.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
