@echo off
cd /d "%~dp0"

echo.
echo ============================================
echo       STEAM GRID TOOL - COMPILADOR
echo ============================================
echo.

echo === Buscando Python ===

set "PYTHON_EXE="

REM Buscar Python mediante el launcher de Windows
where py >nul 2>&1

if not errorlevel 1 (
    set "PYTHON_EXE=py"
    goto python_encontrado
)

REM Buscar python.exe en ubicaciones comunes
if exist "%LocalAppData%\Programs\Python\Python313\python.exe" (
    set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python313\python.exe"
    goto python_encontrado
)

if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
    set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python312\python.exe"
    goto python_encontrado
)

if exist "%LocalAppData%\Programs\Python\Python311\python.exe" (
    set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python311\python.exe"
    goto python_encontrado
)

if exist "%ProgramFiles%\Python313\python.exe" (
    set "PYTHON_EXE=%ProgramFiles%\Python313\python.exe"
    goto python_encontrado
)

if exist "%ProgramFiles%\Python312\python.exe" (
    set "PYTHON_EXE=%ProgramFiles%\Python312\python.exe"
    goto python_encontrado
)

if exist "%ProgramFiles%\Python311\python.exe" (
    set "PYTHON_EXE=%ProgramFiles%\Python311\python.exe"
    goto python_encontrado
)

echo.
echo ERROR: No se pudo encontrar Python.
echo.
echo Instala Python o configura la ruta manualmente.
echo.
pause
exit /b 1


:python_encontrado

echo Python encontrado:
echo %PYTHON_EXE%

echo.
echo === Instalando dependencias ===

"%PYTHON_EXE%" -m pip install --upgrade pip
if errorlevel 1 goto error

"%PYTHON_EXE%" -m pip install requests pillow pyinstaller
if errorlevel 1 goto error


echo.
echo === Limpiando compilaciones anteriores ===

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist SteamGridTool.spec del /q SteamGridTool.spec


echo.
echo === Generando SteamGridTool.exe ===

"%PYTHON_EXE%" -m PyInstaller ^
    --onefile ^
    --windowed ^
    --clean ^
    --noconfirm ^
    --icon="estrella.ico" ^
    --name="SteamGridTool" ^
    steamgrid_tool.py

if errorlevel 1 goto error


echo.
echo ============================================
echo.
echo   COMPILACION TERMINADA
echo.
echo   dist\SteamGridTool.exe
echo.
echo ============================================
echo.

pause
exit /b 0


:error

echo.
echo ============================================
echo   HUBO UN ERROR
echo ============================================
echo.
echo Revisa el mensaje anterior.
echo.
pause
exit /b 1