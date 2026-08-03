@echo off
rem Запуск бота на рабочей станции. Останов — Ctrl+C.
cd /d "%~dp0"
uv run --python 3.12 python -m bugbot
