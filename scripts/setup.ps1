# Create the virtual environment and install dependencies.
# On Windows keep the venv at a SHORT path: deep project folders exceed the 260-character path limit.
param([string]$Venv = ".venv")
uv venv $Venv --python 3.12
uv pip install --python "$Venv\Scripts\python.exe" -e ".[eval,dev]"
if (-not (Test-Path .env)) { Copy-Item .env.example .env; Write-Host "Created .env - add your API keys." }
