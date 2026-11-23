@echo off
setlocal
cd /d "%~dp0"
call "%~dp0toolkit\setup_sci_env.bat" %*
set "SETUP_EXIT=%ERRORLEVEL%"
if not "%SETUP_EXIT%"=="0" (
    echo.
    echo Setup did not complete. Review the error above.
    pause
)
endlocal & exit /b %SETUP_EXIT%
