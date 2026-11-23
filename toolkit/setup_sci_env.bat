@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "APP_ROOT=%~dp0.."
for %%I in ("%APP_ROOT%") do set "APP_ROOT=%%~fI"

set "SCI_ROOT=%LocalAppData%\Sci"
set "ENV_ROOT=%SCI_ROOT%\env"
set "BIN_ROOT=%SCI_ROOT%\bin"
set "APP_DATA_ROOT=%SCI_ROOT%\apps\Raman Phase Finder"
set "LOG_ROOT=%SCI_ROOT%\logs\Raman Phase Finder"
set "DOWNLOAD_ROOT=%SCI_ROOT%\downloads"
set "UPDATE_ROOT=%SCI_ROOT%\updates"
set "LOG_FILE=%LOG_ROOT%\setup.log"

if not exist "%SCI_ROOT%" mkdir "%SCI_ROOT%"
if not exist "%BIN_ROOT%" mkdir "%BIN_ROOT%"
if not exist "%LOG_ROOT%" mkdir "%LOG_ROOT%"
if not exist "%DOWNLOAD_ROOT%" mkdir "%DOWNLOAD_ROOT%"
if not exist "%UPDATE_ROOT%" mkdir "%UPDATE_ROOT%"

echo [%date% %time%] Starting Sci environment setup for Raman > "%LOG_FILE%"
echo Application root: %APP_ROOT%>> "%LOG_FILE%"
echo Sci root: %SCI_ROOT%>> "%LOG_FILE%"
echo Runtime env root: %ENV_ROOT%>> "%LOG_FILE%"
echo Application data: %APP_DATA_ROOT%>> "%LOG_FILE%"

echo [1/4] Checking Python 3.11 or 3.12 and the shared Sci environment...
echo Setup log: "%LOG_FILE%"

call :check_windows_version
if errorlevel 1 exit /b 1

if exist "%ENV_ROOT%\Scripts\python.exe" (
    "%ENV_ROOT%\Scripts\python.exe" -c "import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] < (3,13) else 1)" >nul 2>nul
    if errorlevel 1 (
        echo ERROR: Existing Sci Python cannot be launched. Environment was preserved.
        goto failed
    )
    goto environment_ready
)
call :find_python
if errorlevel 1 (
    call :install_python
    if errorlevel 1 (
        echo Python 3.11+ is required. Install Python and run setup again.
        echo Python 3.11+ not found.>> "%LOG_FILE%"
        exit /b 1
    )
    call :find_python
)

if errorlevel 1 (
    echo Python 3.11+ is installed but could not be launched.
    echo Python launch failed after install.>> "%LOG_FILE%"
    exit /b 1
)

echo Using Python: %PYTHON_CMD%
echo Using Python: %PYTHON_CMD%>> "%LOG_FILE%"

if not exist "%ENV_ROOT%\Scripts\python.exe" (
    echo Creating Sci environment...
    echo Creating venv at %ENV_ROOT%>> "%LOG_FILE%"
    %PYTHON_CMD% -m venv "%ENV_ROOT%"
    if errorlevel 1 goto failed
)

if not exist "%ENV_ROOT%\Scripts\python.exe" (
    echo Failed to create Sci environment.
    echo venv creation failed.>> "%LOG_FILE%"
    exit /b 1
)

:environment_ready
"%ENV_ROOT%\Scripts\python.exe" -B "%APP_ROOT%\Vibrational_Finder\vibrational_finder\core\app_paths.py"
if errorlevel 1 goto failed
echo [2/4] Checking pip and build tools...
"%ENV_ROOT%\Scripts\python.exe" -m pip --version
if errorlevel 1 (
    "%ENV_ROOT%\Scripts\python.exe" -m ensurepip --upgrade
    if errorlevel 1 goto failed
)
"%ENV_ROOT%\Scripts\python.exe" -c "import importlib.metadata as m; assert int(m.version('setuptools').split('.')[0]) >= 68; m.version('wheel')" >nul 2>nul
if errorlevel 1 (
    "%ENV_ROOT%\Scripts\python.exe" -m pip --log "%LOG_FILE%" install --disable-pip-version-check --timeout 90 --retries 3 --prefer-binary "setuptools>=68" wheel
    if errorlevel 1 goto failed
)

echo [3/4] Installing and testing Raman dependencies...
call "%ENV_ROOT%\Scripts\python.exe" -u "%APP_ROOT%\toolkit\install_sci_requirements.py" --root "%APP_ROOT%" --log "%LOG_FILE%"
if errorlevel 1 goto failed

echo [4/4] Updating Raman launch commands...
call :write_launchers
if errorlevel 1 goto failed
echo Updating user PATH...>> "%LOG_FILE%"
call :ensure_user_path "%BIN_ROOT%"
if errorlevel 1 goto failed

echo [%date% %time%] Sci environment setup complete.>> "%LOG_FILE%"
echo Sci environment is ready.
exit /b 0

:write_launchers
> "%BIN_ROOT%\sci-python.cmd" echo @echo off
>> "%BIN_ROOT%\sci-python.cmd" echo "%ENV_ROOT%\Scripts\python.exe" %%*
"%SystemRoot%\System32\cscript.exe" //nologo "%APP_ROOT%\launch_raman_phase_finder.vbs" --register-user
exit /b %ERRORLEVEL%

:ensure_user_path
set "BIN_PATH=%~1"
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $bin=$env:BIN_PATH; $path=[Environment]::GetEnvironmentVariable('Path','User'); $parts=@($path -split ';' | Where-Object { $_ }); if ($parts -notcontains $bin) { [Environment]::SetEnvironmentVariable('Path',(($parts + $bin) -join ';'),'User') }"
exit /b %ERRORLEVEL%

:check_windows_version
ver | findstr /r /c:" 10\." >nul
if errorlevel 1 (
    echo Raman requires Windows 10 or Windows 11.
    echo Unsupported Windows version.>> "%LOG_FILE%"
    exit /b 1
)
exit /b 0

:find_python
set "PYTHON_CMD="
set "PYTHON_TEST=import sys; raise SystemExit(0 if sys.version_info >= (3, 11) and sys.version_info < (3, 13) else 1)"
for %%V in (Python311 Python312) do (
    for %%D in ("%LocalAppData%\Programs\Python\%%V" "%ProgramFiles%\%%V") do (
        if exist "%%~D\python.exe" (
            "%%~D\python.exe" -c "%PYTHON_TEST%" >nul 2>nul
            if not errorlevel 1 (
                set PYTHON_CMD="%%~D\python.exe"
                exit /b 0
            )
        )
    )
)
py -3.11 -c "%PYTHON_TEST%" >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=py -3.11"
    exit /b 0
)
py -3.12 -c "%PYTHON_TEST%" >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=py -3.12"
    exit /b 0
)
python -c "%PYTHON_TEST%" >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    exit /b 0
)
exit /b 1

:install_python
where winget >nul 2>nul
if errorlevel 1 goto install_python_direct
echo Python 3.11+ was not found. Installing with winget...
echo Installing Python 3.11 with winget...>> "%LOG_FILE%"
winget install --id Python.Python.3.11 -e --source winget --scope user --accept-package-agreements --accept-source-agreements
if not errorlevel 1 exit /b 0

:install_python_direct
set "PYTHON_INSTALLER_DIR=%SCI_ROOT%\downloads"
set "PYTHON_INSTALLER=%PYTHON_INSTALLER_DIR%\python-3.11.9-amd64.exe"
set "PYTHON_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
if not exist "%PYTHON_INSTALLER_DIR%" mkdir "%PYTHON_INSTALLER_DIR%"
echo Downloading Python 3.11.9 from python.org...>> "%LOG_FILE%"
echo Downloading Python 3.11.9 from python.org...
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri $env:PYTHON_URL -OutFile $env:PYTHON_INSTALLER -UseBasicParsing -TimeoutSec 180; $signature=Get-AuthenticodeSignature -LiteralPath $env:PYTHON_INSTALLER; if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'CN=Python Software Foundation(?:,|$)') { throw 'The Python installer signature could not be verified.' }"
if errorlevel 1 exit /b 1
if not exist "%PYTHON_INSTALLER%" exit /b 1
echo Installing Python 3.11.9 for current user...>> "%LOG_FILE%"
echo Installing Python 3.11.9 for current user...
"%PYTHON_INSTALLER%" /passive InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_pip=1 Include_tcltk=1 Include_test=0 Shortcuts=0 /log "%LOG_ROOT%\python-install.log"
exit /b %ERRORLEVEL%

:failed
echo Raman setup failed. See log: "%LOG_FILE%"
echo [%date% %time%] setup failed.>> "%LOG_FILE%"
exit /b 1
