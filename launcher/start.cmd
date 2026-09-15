@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0.."
title SO Engine 3.3

set "ENGINE_PYTHON=%CD%\.venv\Scripts\python.exe"

if not exist "%ENGINE_PYTHON%" (
    echo [SO Engine] Preparing the local environment...
    where uv >nul 2>nul || (
        echo ERROR: uv was not found. Install uv and try again.
        pause
        exit /b 1
    )
    uv sync || (
        echo ERROR: failed to prepare the environment.
        pause
        exit /b 1
    )
)

if /i "%~1"=="--self-test" (
    "%ENGINE_PYTHON%" -m so_engine --self-test
    exit /b %ERRORLEVEL%
)

set "ITEMS_FILE=%~1"
if not defined ITEMS_FILE (
    echo.
    echo ==============================================
    echo              SO ENGINE 3.3
    echo ==============================================
    echo Drag a UTF-8 TXT item file onto the shortcut,
    echo or paste the full file path below.
    echo.
    set /p "ITEMS_FILE=Item file: "
)

if not defined ITEMS_FILE (
    echo ERROR: no file was selected.
    pause
    exit /b 2
)

set "ITEMS_FILE=%ITEMS_FILE:"=%"
if not exist "%ITEMS_FILE%" (
    echo ERROR: file not found: "%ITEMS_FILE%"
    pause
    exit /b 2
)

for /f %%I in ('powershell.exe -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "RUN_ID=%%I"
if not defined RUN_ID set "RUN_ID=latest"
set "RUN_DIR=runtime\user-runs\%RUN_ID%"

"%ENGINE_PYTHON%" -m so_engine --self-test >nul || (
    echo ERROR: the SO Engine internal self-test failed.
    pause
    exit /b 1
)

echo.
echo Processing items...
echo Results: %CD%\%RUN_DIR%
echo.

"%ENGINE_PYTHON%" -m so_engine ^
    --items-file "%ITEMS_FILE%" ^
    --run-dir "%RUN_DIR%" ^
    --output result.txt ^
    --checkpoint checkpoint.json ^
    --failed-output failed.txt ^
    --skipped-output skipped.txt ^
    --audit-file audit.json ^
    --proxy-file "%CD%\proxies.txt"

set "EXIT_CODE=%ERRORLEVEL%"

if "%EXIT_CODE%"=="0" (
    echo.
    echo DONE: every item was processed.
    explorer.exe "%CD%\%RUN_DIR%"
) else if "%EXIT_CODE%"=="3" (
    echo.
    echo INCOMPLETE: inspect failed.txt and skipped.txt.
    explorer.exe "%CD%\%RUN_DIR%"
) else (
    echo.
    echo ERROR: SO Engine exited with code %EXIT_CODE%.
)

echo.
pause
exit /b %EXIT_CODE%
