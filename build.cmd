@echo off
REM Build Glide into a single dist\glide.exe
where pyinstaller >nul 2>&1 || python -m pip install pyinstaller pynput
pyinstaller --onefile --name glide --icon assets\glide.ico ^
    --console --clean --noconfirm glide.py
echo.
echo Done. Your executable is at: dist\glide.exe
pause
