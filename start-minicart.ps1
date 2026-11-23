
$projectRoot = $PSScriptRoot

# Start PostgreSQL
Write-Host "Starting PostgreSQL..." -ForegroundColor Cyan
docker compose up -d postgres

if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to start PostgreSQL." -ForegroundColor Red
    exit 1
}

# Wait until PostgreSQL is ready
Write-Host "Waiting for PostgreSQL..."

do {
    Start-Sleep -Seconds 2
    docker compose exec -T postgres pg_isready -U minicart_admin -d minicart_auth_db
} until ($LASTEXITCODE -eq 0)

# Start Auth Service
Start-Process powershell.exe -WorkingDirectory $projectRoot -ArgumentList @(
    "-NoExit",
    "-Command",
    "& '.\auth-service\.venv\Scripts\python.exe' 'auth-service/server.py'"
)

# Start Product Service
Start-Process powershell.exe -WorkingDirectory $projectRoot -ArgumentList @(
    "-NoExit",
    "-Command",
    "& '.\product-service\.venv\Scripts\python.exe' 'product-service/server.py'"
)

# Start Order Service
Start-Process powershell.exe -WorkingDirectory $projectRoot -ArgumentList @(
    "-NoExit",
    "-Command",
    "& '.\order-service\.venv\Scripts\python.exe' 'order-service/server.py'"
)

# Start API Gateway
Start-Process powershell.exe -WorkingDirectory $projectRoot -ArgumentList @(
    "-NoExit",
    "-Command",
    "& '.\api-gateway\.venv\Scripts\python.exe' -m uvicorn gateway.main:app --app-dir api-gateway --host 127.0.0.1 --port 8000"
)

Write-Host "MiniCart services launched!" -ForegroundColor Green
