#!/usr/bin/env bash
# ============================================================
# k8s/deploy.sh -- deploy the whole stack to a self-hosted
# single-node Kubernetes cluster (minikube).
#
# Prerequisites: minikube and kubectl installed.
# Run from the repo root:   bash k8s/deploy.sh
# ============================================================
set -euo pipefail

echo "==> 1/6  Starting minikube"
minikube status >/dev/null 2>&1 || minikube start

echo "==> 2/6  Enabling the ingress addon"
minikube addons enable ingress

echo "==> 3/6  Pointing docker at minikube's daemon"
# Builds now go straight into the cluster's image store -- no registry.
eval "$(minikube docker-env)"

echo "==> 4/6  Building the three service images"
docker build -t llm-obs-ingestion:latest -f services/ingestion-api/Dockerfile .
docker build -t llm-obs-chat:latest      -f services/chat-api/Dockerfile .
docker build -t llm-obs-frontend:latest  -f frontend/Dockerfile .

echo "==> 5/6  Applying Kubernetes manifests"
kubectl apply -f k8s/manifests.yaml

echo "==> 6/6  Waiting for all deployments to become available"
kubectl -n llm-obs rollout status deploy/postgres      --timeout=120s
kubectl -n llm-obs rollout status deploy/redis         --timeout=120s
kubectl -n llm-obs rollout status deploy/ingestion-api --timeout=120s
kubectl -n llm-obs rollout status deploy/chat-api      --timeout=120s
kubectl -n llm-obs rollout status deploy/frontend      --timeout=120s

echo
echo "============================================================"
echo " Deployed. Pods:"
kubectl -n llm-obs get pods
echo
echo " Open the UI with EITHER:"
echo "   minikube service frontend -n llm-obs        # opens a browser"
echo "   -- or via the Ingress: add '\$(minikube ip) llm-obs.local'"
echo "      to /etc/hosts, then visit http://llm-obs.local"
echo
echo " Tear down with:  kubectl delete namespace llm-obs"
echo "============================================================"
