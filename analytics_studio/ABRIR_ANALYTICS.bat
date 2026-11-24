@echo off
title PodcastForge Analytics Studio
cd /d "%~dp0\.."
echo Iniciando PodcastForge Analytics Studio en http://127.0.0.1:8765 ...
start "" http://127.0.0.1:8765
".venv\Scripts\python.exe" -m analytics_studio.server
pause
