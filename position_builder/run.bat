@echo off
REM Position Builder Startup Script for Windows

echo.
echo ========================================
echo   Deribit Position Builder
echo ========================================
echo.

echo Installing dependencies...
pip install -r requirements.txt

echo.
echo Starting Flask backend...
echo Backend running on: http://localhost:5000
echo.
echo.
echo NEXT STEPS:
echo 1. Open a NEW terminal window
echo 2. Run: cd d:\Options_Trading\position_builder
echo 3. Run: python -m http.server 8000
echo 4. Open browser: http://localhost:8000
echo.
echo Press Ctrl+C in this window to stop the backend
echo.

python app.py
pause
