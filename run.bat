@echo off
title YouTube Downloader Server
color 0c

cd /d "%~dp0"

echo ========================================================
echo    Checking and updating yt-dlp to latest version...
echo ========================================================
python -m pip install --upgrade yt-dlp --quiet

echo.
echo ========================================================
echo    Starting Media Downloader Server (FastAPI)...
echo ========================================================

python app.py

pause