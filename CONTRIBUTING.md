# Contributing

Thanks for interest in Apron. Inference stays **outside** this project — contributions should keep the gateway and workers model-agnostic.

## Before you start

1. Read [README.md](README.md) (desktop golden path) and [AGENTS.md](AGENTS.md) (layout, seats, env).
2. Prefer small, focused PRs. Match existing style; don’t expand scope into LLM loops or auto-CAPTCHA.
3. New product features should attach at gateway edges (e.g. recording, auth) — avoid bloating `gateway/pool.py` (see seat helpers: `cloud_phone_seats`, `physical_phone_seats`, `burst_seats`).
4. Do not put LLM calls in the gateway or workers. Breaking changes to guaranteed `/v1` behavior need a new API prefix or an update to [docs/v1.md](docs/v1.md).

## Dev loop

```bash
cp -n .env.example .env
docker compose up --build -d

# Gateway unit tests (same set as CI)
cd gateway
pip install -r requirements-dev.txt
WORKER_INVENTORY_PATH=./workers.json REQUIRE_WORKER_INVENTORY=1 \
  COMPUTER_API_TOKEN= RECORDING_DIR=/tmp/cwa-recordings \
  python -m pytest \
    test_pool_burst.py test_pool_phone_lease.py test_pool_register.py \
    test_recording.py test_recording_api.py test_tenancy.py \
    providers/test_phone_harness.py -q
```

Compose golden path (lease → act → recycle → recordings):

```bash
./scripts/oss-smoke.sh
```

Full release checklist: [docs/oss-smoke.md](docs/oss-smoke.md). Bind examples: [BINDING.md](BINDING.md).

## Pull requests

- Describe **why**, not only what.
- Include how you tested (compose smoke and/or pytest). CI must stay green.
- Don’t commit secrets (`.env`, API keys, kubeconfigs).

## Security

Do **not** open public issues for vulnerabilities. See [SECURITY.md](SECURITY.md).
