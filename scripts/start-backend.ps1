$ErrorActionPreference = "Stop"
Push-Location "$PSScriptRoot\..\backend"
uvicorn app.main:app --reload --port 8000
