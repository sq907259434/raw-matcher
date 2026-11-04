@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/4] 检查 Python 与 PyInstaller…
python --version >nul 2>&1
if errorlevel 1 (
  echo     没找到 python 命令，请先安装 Python 3.8+ 并勾选 Add to PATH。
  pause
  exit /b 1
)
python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
  echo     正在安装 PyInstaller…
  python -m pip install --upgrade pyinstaller
)

echo [2/4] 准备图标…
if not exist app.ico python make_icon.py

echo [3/4] 打包 exe…
python -m PyInstaller --noconfirm --clean --onefile --noconsole ^
  --name "RAW选片工具" --icon app.ico ^
  --add-data "ui.html;." ^
  --distpath dist --workpath build --specpath . app.py

echo [4/4] 清理中间文件…
if exist build rmdir /s /q build
if exist "RAW选片工具.spec" del /q "RAW选片工具.spec"

if exist "dist\RAW选片工具.exe" (
  echo.
  echo 完成：%~dp0dist\RAW选片工具.exe
) else (
  echo 打包失败，请检查上面的报错。
)

pause
