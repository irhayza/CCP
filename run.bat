@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Cheat Clip Pro Max

rem --- cari Python 3.10+ (launcher "py" lebih andal daripada "python" di Windows) ---
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 goto oldpython

rem --- lingkungan virtual + dependensi (hanya pertama kali) ---
if not exist ".venv\Scripts\python.exe" (
  echo Menyiapkan lingkungan Python ^(sekali saja^)...
  %PY% -m venv .venv
  if errorlevel 1 goto fail
)
if exist ".venv\.deps_ok" goto run

echo Memasang dependensi ^(sekali saja, butuh internet^)...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
type nul > ".venv\.deps_ok"

:run
".venv\Scripts\python.exe" run.py %*
if errorlevel 1 pause
exit /b %errorlevel%

:nopython
echo.
echo Python 3.10 atau lebih baru belum terpasang.
echo Unduh dari https://www.python.org/downloads/ lalu CENTANG "Add python.exe to PATH" saat instalasi.
echo.
pause
exit /b 1

:oldpython
echo.
echo Versi Python terlalu lama. Dibutuhkan 3.10 atau lebih baru: https://www.python.org/downloads/
echo.
pause
exit /b 1

:fail
echo.
echo Gagal menyiapkan aplikasi. Baca pesan error di atas. Hapus folder ".venv" lalu coba lagi bila perlu.
echo.
pause
exit /b 1
