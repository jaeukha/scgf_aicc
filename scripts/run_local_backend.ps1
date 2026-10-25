$env:PROVIDER_MODE = "local"
$env:COACH_MODE = "internal"
$env:COACH_BASE_URL = "http://127.0.0.1:8101"
& "$PSScriptRoot\..\.venv-app\Scripts\python.exe" -m uvicorn aicc.main:app --host 127.0.0.1 --port 8000
