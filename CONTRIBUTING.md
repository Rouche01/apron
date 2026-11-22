# Contributing

Thanks for interest in Computer Runtime. Inference stays **outside** this project — contributions should keep the gateway and workers model-agnostic.

## Before you start

1. Read [README.md](README.md) (desktop golden path) and [AGENTS.md](AGENTS.md) (layout, seats, env).
2. Prefer small, focused PRs. Match existing style; don’t expand scope into LLM loops or auto-CAPTCHA.
3. New product features should attach at gateway edges (e.g. recording, auth) — avoid bloating `gateway/pool.py` (see seat helpers: `cloud_phone_seats`, `physical_phone_seats`, `burst_seats`).

## Dev loop

```bash
cp -n .env.example .env
docker compose up --build -d

# Gateway unit tests (pool / providers)
cd gateway && python -m pytest test_pool_burst.py test_pool_phone_lease.py test_pool_register.py providers/test_phone_harness.py -q
```

Bind examples and tool schemas: [BINDING.md](BINDING.md).

## Pull requests

- Describe **why**, not only what.
- Include how you tested (compose smoke and/or pytest).
- Don’t commit secrets (`.env`, API keys, kubeconfigs).

## Security

Do **not** open public issues for vulnerabilities. See [SECURITY.md](SECURITY.md).
