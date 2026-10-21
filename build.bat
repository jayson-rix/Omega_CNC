@echo off
setlocal

:: ============================================================
::  Omega CNC Processor  Windows Build Script
::  Produces a standalone .exe folder via PyInstaller.
::
::  Output folder (distribute the ENTIRE contents):
::    dist\Omega_CNC_Processor\
::
::  tools.json and per-machine machine_libraries folders inside that
::  folder are the live data updated by explicit Library save actions.
:: ============================================================

echo.
echo  ================================================
echo   Omega CNC Processor  -  Build
echo  ================================================
echo.

:: Run from the folder containing this script
cd /d "%~dp0"

:: ---- Python check --------------------------------------------------
:: Prefer the Windows Python Launcher, which remains available even when
:: Windows updates remove python.exe from PATH. Fall back to python.exe.
set "PYTHON_CMD="
py -3 --version >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3"

if not defined PYTHON_CMD (
    python --version >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo  ERROR: Python 3 was not found.
    echo  Install Python 3.10+ from https://python.org and try again.
    pause
    exit /b 1
)

echo  Using:
%PYTHON_CMD% --version

:: ---- Install / verify dependencies ---------------------------------
echo  Checking dependencies...
%PYTHON_CMD% -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo  ERROR: pip install failed. Check your Python environment.
    pause
    exit /b 1
)

:: Verify PyInstaller is importable
%PYTHON_CMD% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: PyInstaller still not found after install attempt.
    echo  Try running:  python -m pip install -r requirements.txt
    pause
    exit /b 1
)

:: ---- Clean previous output -----------------------------------------
echo  Cleaning previous build...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

:: ---- Build ---------------------------------------------------------
echo.
echo  Running PyInstaller...
echo.
%PYTHON_CMD% -m PyInstaller cnc_preprocessor.spec --noconfirm

if errorlevel 1 (
    echo.
    echo  ERROR: Build failed. Review the output above.
    pause
    exit /b 1
)

:: ---- Smoke-test the clean release ----------------------------------
echo.
echo  Smoke-testing clean machine libraries...
%PYTHON_CMD% windows_package_smoke.py
if errorlevel 1 (
    echo.
    echo  ERROR: Packaged app smoke check failed.
    pause
    exit /b 1
)

:: ---- Done ----------------------------------------------------------
echo.
echo  ================================================
echo   BUILD SUCCESSFUL
echo.
echo   Distributable folder:
echo     %~dp0dist\Omega_CNC_Processor\
echo.
echo   Send the ENTIRE folder to the machine  -
echo   the .exe will not run without the files
echo   that sit alongside it.
echo.
echo   Each machine gets its own empty profiles.json and complete
echo   DXF suite folders on first launch. Only explicit SIDE or
echo   machining saves create profile records.
echo  ================================================
echo.

pause
