@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo Python o'rnatilmagan: https://www.python.org/downloads/ dan o'rnating ^(Add to PATH belgisini qo'ying^) & pause & exit /b)
if not exist .venv (python -m venv .venv)
call .venv\Scripts\activate
pip install -q -r requirements.txt
python sozlash.py
pause
