# k3s burst workers

Reserved seats stay on **Docker Compose**. On-demand burst seats are **Kubernetes Jobs** on a single-node **k3s** cluster.

Gateway talks to the cluster through `SEAT_BURST_BACKEND=k3s` (`gateway/workers/k3s.py`).

## One-time host setup (ThinkPad / deploy box)

```bash
# Install k3s (writes kubeconfig to /etc/rancher/k3s/k3s.yaml)
curl -sfL https://get.k3s.io | sh -

# Make kubeconfig usable by your user / gateway container
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/k3s.yaml
sudo chown "$USER" ~/.kube/k3s.yaml
export KUBECONFIG=~/.kube/k3s.yaml

kubectl apply -f deploy/k3s/namespace.yaml

# Import the Compose-built desktop image into k3s containerd (local dogfood):
docker save computer-with-agent-worker:local -o /tmp/worker.tar
sudo k3s ctr images import /tmp/worker.tar
```

## Gateway env (burst still opt-in)

```bash
SEAT_BURST_BACKEND=k3s
DESKTOP_BURST_MAX=2          # 0 = never spawn
KUBECONFIG=/path/to/k3s.yaml
SEAT_BURST_NAMESPACE=computer-burst
SEAT_BURST_DESKTOP_IMAGE=computer-with-agent-worker:local
GATEWAY_BURST_VNC_BASE=http://localhost:30080   # public HITL origin you route to Pods
```

Pool wiring is live: reserved seats are claimed first; when full and `DESKTOP_BURST_MAX > 0` with `SEAT_BURST_BACKEND=k3s`, the gateway spawns a Job and registers an ephemeral seat. Recycle / parked-idle destroys the Job.

## HITL (noVNC)

Each Job gets a Service (`control:8000`, `novnc:6080`). Expose Watch via:

- **NodePort / Ingress / Caddy** on the host → `GATEWAY_BURST_VNC_BASE`, or
- Port-forward for debug: `kubectl -n computer-burst port-forward svc/<job> 6080:6080`

## Phone burst

Not in phase 1 templates. Later: phone Job image + `CloudPhoneProvider.provision` after Pod ready.

## Why not Nomad

Kubernetes is the common “standard” orchestrator; k3s keeps it runnable on one machine. Nomad is fine but would be a second ops dialect for the same Jobs-vs-warm-pool split.
