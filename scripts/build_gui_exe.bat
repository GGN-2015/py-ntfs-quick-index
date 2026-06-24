@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0\.."

if /I not "%OS%"=="Windows_NT" (
  echo ERROR: This build script only supports Windows.
  exit /b 1
)

if /I "%PROCESSOR_ARCHITECTURE%"=="AMD64" goto arch_ok
if /I "%PROCESSOR_ARCHITEW6432%"=="AMD64" goto arch_ok
echo ERROR: py-ntfs-quick-index only supports amd64 / x86_64 Windows.
exit /b 1

:arch_ok
if defined PYTHON (
  set "PYTHON_CMD=%PYTHON%"
) else (
  where py >nul 2>nul
  if errorlevel 1 (
    set "PYTHON_CMD=python"
  ) else (
    set "PYTHON_CMD=py -3"
  )
)

%PYTHON_CMD% -c "import platform,sys; m=platform.machine().lower(); sys.exit(0 if m in ('amd64','x86_64') and sys.maxsize > 2**32 else 1)"
if errorlevel 1 (
  echo ERROR: The selected Python interpreter must be 64-bit amd64 / x86_64.
  exit /b 1
)

set "BUILD_ROOT=%CD%\.build"
set "VENV=%BUILD_ROOT%\gui-exe-venv"
set "WORK_DIR=%BUILD_ROOT%\pyinstaller-work"
set "SPEC_DIR=%BUILD_ROOT%\pyinstaller-spec"
set "DIST_DIR=%CD%\dist\exe"
set "ENTRY=%CD%\scripts\pnqi_gui_entry.py"

if not exist "%BUILD_ROOT%" mkdir "%BUILD_ROOT%"
if not exist "%VENV%\Scripts\python.exe" (
  echo Creating build virtual environment...
  %PYTHON_CMD% -m venv "%VENV%"
  if errorlevel 1 exit /b 1
)

set "VENV_PY=%VENV%\Scripts\python.exe"

echo Installing build tools...
"%VENV_PY%" -m pip install --upgrade pip setuptools wheel pyinstaller
if errorlevel 1 exit /b 1

echo Installing py-ntfs-quick-index from this checkout...
"%VENV_PY%" -m pip install --upgrade -e .
if errorlevel 1 exit /b 1

echo Building single-file GUI executable...
"%VENV_PY%" -m PyInstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name pnqi-gui ^
  --distpath "%DIST_DIR%" ^
  --workpath "%WORK_DIR%" ^
  --specpath "%SPEC_DIR%" ^
  --paths "%CD%\src" ^
  --hidden-import py_admin_launch ^
  --collect-submodules py_admin_launch ^
  "%ENTRY%"
if errorlevel 1 exit /b 1

echo.
echo Built: "%DIST_DIR%\pnqi-gui.exe"
exit /b 0
