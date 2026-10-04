@echo off
setlocal

python -c "import textual" >nul 2>&1
if errorlevel 1 (
    python -m pip install --user textual
)

python "%~dp0internet_archive_search.py" %*
endlocal
