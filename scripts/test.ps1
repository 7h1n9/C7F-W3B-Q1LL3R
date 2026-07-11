$ErrorActionPreference = "Stop"
Push-Location "$PSScriptRoot\..\backend"; python -m ruff check .; python -m pytest; Pop-Location
Push-Location "$PSScriptRoot\..\kali-runner"; python -m ruff check .; python -m pytest; Pop-Location
Push-Location "$PSScriptRoot\..\frontend"; npm run build; Pop-Location
Push-Location "$PSScriptRoot\..\codex-bridge"; npm run build; Pop-Location
