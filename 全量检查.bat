@echo off
setlocal
title LAN Chat Room Full Check
cd /d "%~dp0"

rem ==============================================================
rem  KEEP THIS FILE PURE ASCII.
rem  cmd.exe reads a .bat byte by byte with the active code page, so a
rem  single non-ASCII character shifts every line boundary and you get
rem  errors like  "'xx' is not recognized as an internal command".
rem  Every Chinese message is printed by tests\full_check.py in UTF-8,
rem  which is why the code page is switched below.
rem ==============================================================

if not exist "tests\full_check.py" (
  echo [ERROR] tests\full_check.py was not found next to this file.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] The Python environment is missing.
  echo Open the control panel once to create it, then run this file again.
  pause
  exit /b 1
)

chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"

".venv\Scripts\python.exe" "%~dp0tests\full_check.py" %*
set "CODE=%ERRORLEVEL%"

echo.
if "%CODE%"=="0" (
  echo [DONE] All checks passed.
) else if "%CODE%"=="2" (
  echo [DONE] Environment problem - nothing was checked.
) else (
  echo [DONE] Some checks failed. Exit code %CODE%.
)
pause
exit /b %CODE%
