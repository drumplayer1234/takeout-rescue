@echo off
rem Builds dist\TakeoutRescue.exe and dist\TakeoutRescue-v<version>-windows.zip
rem Needs: python -m pip install pyinstaller piexif
setlocal
cd /d "%~dp0"

for /f "delims=" %%v in ('python -c "import takeout_rescue; print(takeout_rescue.__version__)"') do set VERSION=%%v
if not defined VERSION (
    echo ERROR: could not read the version from takeout_rescue\__init__.py
    exit /b 1
)
echo Building Takeout Rescue %VERSION%

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name TakeoutRescue ^
    --paths . ^
    --hidden-import piexif ^
    --distpath dist --workpath build --specpath build ^
    packaging\launch.py
if errorlevel 1 (
    echo ERROR: PyInstaller failed
    exit /b 1
)
if not exist "dist\TakeoutRescue.exe" (
    echo ERROR: dist\TakeoutRescue.exe was not created
    exit /b 1
)

set ZIP=dist\TakeoutRescue-v%VERSION%-windows.zip
if exist "%ZIP%" del "%ZIP%"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\TakeoutRescue.exe' -DestinationPath '%ZIP%' -Force"
if errorlevel 1 (
    echo ERROR: could not create the zip
    exit /b 1
)
if not exist "%ZIP%" (
    echo ERROR: %ZIP% was not created
    exit /b 1
)

echo.
echo Done:
echo   dist\TakeoutRescue.exe
echo   %ZIP%
exit /b 0
