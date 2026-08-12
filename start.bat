@echo off
setlocal

set "VENV_PY=%~dp0nfse_zip_receiver_venv\Scripts\python.exe"
set "VENV_PIP=%~dp0nfse_zip_receiver_venv\Scripts\pip.exe"

if not exist "%VENV_PY%" (
  python -m venv "%~dp0nfse_zip_receiver_venv"
  "%VENV_PIP%" install -r "%~dp0requirements.txt"
)

if exist "%~dp0.env" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0.env") do (
    set "%%A=%%B"
  )
)

echo NFS-e ZIP Receiver — destino: %NFSE_DEST_DIR%
"%VENV_PY%" "%~dp0app.py"
