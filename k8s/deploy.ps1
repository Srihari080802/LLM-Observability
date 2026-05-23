# ============================================================
# k8s/deploy.ps1 -- deploy the whole stack to minikube via PowerShell
# Run from the repo root:   .\k8s\deploy.ps1
# ============================================================
$ErrorActionPreference = "Stop"

Write-Host "==> 1/6  Starting minikube..." -ForegroundColor Cyan
$status = minikube status --format "{{.Host}}" 2>$null
if ($status -ne "Running") {
    minikube start --driver=docker
}

Write-Host "==> 2/6  Enabling the ingress addon..." -ForegroundColor Cyan
minikube addons enable ingress

Write-Host "==> 3/6  Pointing docker at minikube's daemon..." -ForegroundColor Cyan
& minikube -p minikube docker-env --shell powershell | Invoke-Expression

Write-Host "==> 4/6  Building the three service images..." -ForegroundColor Cyan
docker build -t llm-obs-ingestion:latest -f services/ingestion-api/Dockerfile .
docker build -t llm-obs-chat:latest      -f services/chat-api/Dockerfile .
docker build -t llm-obs-frontend:latest  -f frontend/Dockerfile .

Write-Host "==> 5/6  Applying Kubernetes secrets and manifests..." -ForegroundColor Cyan
# Ensure namespace exists
kubectl create namespace llm-obs --dry-run=client -o yaml | kubectl apply -f -

# Generate secret dynamically from .env if it exists
if (Test-Path ".env") {
    Write-Host "Found .env file. Injecting keys into llm-secrets..." -ForegroundColor Green
    kubectl create secret generic llm-secrets --namespace=llm-obs --from-env-file=.env --dry-run=client -o yaml | kubectl apply -f -
} else {
    Write-Host "Warning: .env file not found. Skipping dynamic secret generation." -ForegroundColor Yellow
}

# Apply the rest of the resources
kubectl apply -f k8s/manifests.yaml

Write-Host "==> 6/6  Waiting for all deployments to become available..." -ForegroundColor Cyan
$deployments = @("postgres", "redis", "ingestion-api", "chat-api", "frontend")
foreach ($deploy in $deployments) {
    kubectl -n llm-obs rollout status "deploy/$deploy" --timeout=120s
}

Write-Host "`n============================================================" -ForegroundColor Green
Write-Host " Deployed smoothly! Pods:" -ForegroundColor Green
kubectl -n llm-obs get pods
Write-Host "`n Open the UI with:" -ForegroundColor Green
Write-Host "   minikube service frontend -n llm-obs" -ForegroundColor Yellow
Write-Host "============================================================" -ForegroundColor Green