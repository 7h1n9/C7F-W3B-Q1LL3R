$ErrorActionPreference = "Stop"
Copy-Item backend\.env.example backend\.env -ErrorAction SilentlyContinue
Copy-Item kali-runner\.env.example kali-runner\.env -ErrorAction SilentlyContinue
Copy-Item codex-bridge\.env.example codex-bridge\.env -ErrorAction SilentlyContinue
Copy-Item frontend\.env.example frontend\.env -ErrorAction SilentlyContinue
Push-Location backend; python -m pip install -e ".[dev]"; Pop-Location
Push-Location kali-runner; python -m pip install -e ".[dev]"; Pop-Location
Push-Location frontend; npm install; Pop-Location
Push-Location codex-bridge; npm install; Pop-Location
