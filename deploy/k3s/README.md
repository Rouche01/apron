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
docker save apron-worker:local -o /tmp/worker.tar
sudo k3s ctr images import /tmp/worker.tar
```

## Gateway env (burst still opt-in)

```bash
SEAT_BURST_BACKEND=k3s
DESKTOP_BURST_MAX=2          # 0 = never spawn
PHONE_BURST_MAX=1            # 0 = never spawn phone workers
PHONE_BURST_PROVIDER=phone-harness
KUBECONFIG=/path/to/k3s.yaml
SEAT_BURST_NAMESPACE=computer-burst
SEAT_BURST_DESKTOP_IMAGE=apron-worker:local
SEAT_BURST_PHONE_IMAGE=apron-phone:local
GATEWAY_BURST_VNC_BASE=http://localhost:30080   # public HITL origin you route to desktop Pods
```

Pool wiring is live: reserved seats are claimed first; when full and `*_BURST_MAX > 0` with `SEAT_BURST_BACKEND=k3s`, the gateway spawns a Job and registers an ephemeral seat. Recycle / parked-idle destroys the Job (and tears down cloud provider sessions for phone).

Import phone image for phone burst:

```bash
docker save apron-phone:local -o /tmp/phone.tar
sudo k3s ctr images import /tmp/phone.tar
```

## HITL (noVNC) — desktop burst

Each desktop Job gets a Service (`control:8000`, `novnc:6080`). Expose Watch via:

- **NodePort / Ingress / Caddy** on the host → `GATEWAY_BURST_VNC_BASE`, or
- Port-forward for debug: `kubectl -n computer-burst port-forward svc/<job> 6080:6080`

## Phone burst

`phone-burst-job.yaml.template` runs the phone worker (`ALLOW_EMPTY_DEVICE=1`, control `:8000`). After Pod ready the pool sets `provider=PHONE_BURST_PROVIDER` and runs `CloudPhoneProvider.provision` + ADB bind. Lease `vnc_url` is the vendor **watch_url** (not scrcpy). Recycle → provider teardown + Job delete.

Phone **lease** overflow uses this Job + `PHONE_BURST_MAX` / `PHONE_BURST_PROVIDER`. Physical phone growth is separate (`POST /v1/devices/register`, `PHONE_WARM_SPAWN_MAX`) — same phone image template, warm seat not ephemeral burst.

See [`.cursor/plans/phone_seat_flows_8a1a7577.plan.md`](../../.cursor/plans/phone_seat_flows_8a1a7577.plan.md) and [`.cursor/plans/elastic_seats.plan.md`](../../.cursor/plans/elastic_seats.plan.md).

## Why not Nomad

Kubernetes is the common “standard” orchestrator; k3s keeps it runnable on one machine. Nomad is fine but would be a second ops dialect for the same Jobs-vs-warm-pool split.
