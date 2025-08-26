# Local Dev Quickstart (Minikube + Bank of Anthos + ai-agent)

This is a concise, repeatable set of steps to stop, start, and validate the full stack locally on Windows PowerShell.

## Prereqs
- Minikube (Docker driver)
- kubectl
- PowerShell 5+

## Stop everything
- Stop port-forwards (Ctrl+C in terminals running them)
- Stop cluster
```powershell
minikube stop
```

## Start everything fresh
1) Start cluster
```powershell
minikube start
```

2) Apply core manifests
```powershell
kubectl apply -f .\extras\jwt\jwt-secret.yaml
kubectl apply -f .\kubernetes-manifests
```

3) Disable cloud-dependent telemetry (local dev)
```powershell
kubectl set env deployment --all ENABLE_TRACING=false
kubectl set env deployment --all ENABLE_METRICS=false
```

4) Build ai-agent image into Minikube and restart it
```powershell
minikube image build -t ai-agent:latest .\src\ai-agent
kubectl rollout restart deployment ai-agent
kubectl rollout status deploy/ai-agent --timeout=180s
```

5) Check pods
```powershell
kubectl get pods -o wide
```

## Open local access
- Frontend (web):
```powershell
kubectl port-forward svc/frontend 18080:80
```
- AI agent (API + UI) in another terminal:
```powershell
kubectl port-forward svc/ai-agent 18088:8080
```

Now visit:
- App: http://localhost:18080
- AI Chat UI: http://localhost:18088/ui
- API docs: http://localhost:18088/docs

## Get a JWT & test chat
- Login at http://localhost:18080
- Copy token from browser DevTools → Application/Storage → Cookies (name: `token`)
- Paste into the Chat UI token box, Save, then send: "What's my balance?"

## Notes
- If you `minikube delete`, rebuild the `ai-agent` image and re-apply manifests.
- Keep ports 18080/18088 free or change the local side of the port-forward.
