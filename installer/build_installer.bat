@echo off
setlocal
cd /d "%~dp0"

set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo Inno Setup 6 was not found under Program Files.
    exit /b 1
)

for /f %%V in ('powershell.exe -NoProfile -Command "(ConvertFrom-Json -InputObject (Get-Content -LiteralPath ..\Vibrational_Finder\app.json -Raw)).version"') do set "APP_VERSION=%%V"
if not defined APP_VERSION exit /b 1
"%ISCC%" "/DAppVersion=%APP_VERSION%" Raman_Phase_Finder.iss
exit /b %ERRORLEVEL%
