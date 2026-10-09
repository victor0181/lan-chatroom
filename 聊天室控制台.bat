@echo off
setlocal
title LAN Chat Room Console
cd /d "%~dp0"

rem ==============================================================
rem  KEEP THIS FILE PURE ASCII.
rem  cmd.exe reads a .bat byte by byte with the active code page, so a
rem  single non-ASCII character shifts every line boundary and you get
rem  errors like  "'xx' is not recognized as an internal command".
rem  All Chinese text lives in launcher.py, which prints it correctly.
rem ==============================================================

if not exist "launcher.py" (
  echo [ERROR] launcher.py was not found next to this file.
  pause
  exit /b 1
)

if exist ".venv\Scripts\python.exe" goto have_venv

echo [1/2] Creating the Python virtual environment, please wait...
set "BOOT="
where py >nul 2>nul && set "BOOT=py -3"
if not defined BOOT (
  where python >nul 2>nul && set "BOOT=python"
)
if not defined BOOT (
  echo [ERROR] Python was not found.
  echo Install Python 3.9 or newer, tick "Add Python to PATH", then run this file again.
  pause
  exit /b 1
)
%BOOT% -m venv .venv
if errorlevel 1 (
  echo [ERROR] Could not create the virtual environment.
  pause
  exit /b 1
)

:have_venv
".venv\Scripts\python.exe" -c "import tkinter" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] This Python has no tkinter module, so the control panel cannot open.
  echo Reinstall Python and keep the "tcl/tk and IDLE" option checked.
  pause
  exit /b 1
)

echo [2/2] Opening the control panel...
start "" ".venv\Scripts\pythonw.exe" "%~dp0launcher.py"
exit /b 0
