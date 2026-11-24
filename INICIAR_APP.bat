@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   PodcastForge Studio - Iniciando aplicacion local
echo ============================================================
echo.

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo [AVISO] Se ha creado .env desde .env.example.
        echo Edita .env con tus claves de OpenRouter y Pexels/Pixabay si aun no las pusiste.
        echo.
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo [INFO] Creando entorno virtual Python por primera vez...
    where uv >nul 2>&1
    if %errorlevel%==0 (
        uv venv .venv --python 3.12
        uv pip install --python .venv\Scripts\python.exe -r requirements.txt
    ) else (
        python -m venv .venv
        .venv\Scripts\python.exe -m pip install --upgrade pip
        .venv\Scripts\pip.exe install -r requirements.txt
    )
    if errorlevel 1 (
        echo [ERROR] No se pudieron instalar las dependencias Python.
        pause
        exit /b 1
    )
)

if not exist "frontend\dist\index.html" (
    if exist "frontend\package.json" (
        echo [INFO] Compilando interfaz web...
        pushd frontend
        call npm install
        call npm run build
        popd
    )
)

set "APP_HOST=127.0.0.1"
set "APP_PORT=8000"
for /f "usebackq tokens=1,2 delims==" %%A in (".env") do (
    if /i "%%A"=="HOST" set "APP_HOST=%%B"
    if /i "%%A"=="PORT" set "APP_PORT=%%B"
)
for /f "tokens=1 delims=# " %%H in ("%APP_HOST%") do set "APP_HOST=%%H"
for /f "tokens=1 delims=# " %%P in ("%APP_PORT%") do set "APP_PORT=%%P"

echo [INFO] Verificando componentes locales...
".venv\Scripts\python.exe" cli.py doctor --skip-api
echo.
echo [INFO] Abriendo http://%APP_HOST%:%APP_PORT% en tu navegador...
echo [INFO] Para detener la aplicacion, cierra esta ventana o pulsa Ctrl+C.
echo.

start "" /b cmd /c "timeout /t 2 /nobreak >nul & start http://%APP_HOST%:%APP_PORT%"
".venv\Scripts\python.exe" -m uvicorn backend.main:app --host %APP_HOST% --port %APP_PORT%
pause
