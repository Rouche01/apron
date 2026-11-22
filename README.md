# Computer Runtime

**Lease exclusive computers for any agent — bring your own model.**

Self-hosted control plane for computer-use: warm Linux desktops (XFCE + Firefox) behind a gateway. Agents call HTTP (screenshot / act); humans watch via noVNC. Inference stays outside this repo.

**Requirements:** Docker with Compose v2. First `up --build` pulls/builds images and can take several minutes. Optional: `jq` for prettier JSON.

## Quick start (desktop)

No Compose profile needed — default stack is gateway + two desktop seats.

```bash
cp -n .env.example .env
docker compose up --build -d

export COMPUTER_API_TOKEN=dev-token   # must match .env
curl -s http://localhost:9000/health

# Lease a desktop seat
curl -s -H "Authorization: Bearer $COMPUTER_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"demo","job_id":"demo-job"}' \
  http://localhost:9000/v1/sessions
# Prefer: … | jq .   if jq is installed
```

Open the lease `vnc_url` for live Watch. Then screenshot → model → action → release:

```bash
SID=…   # session_id from the lease response

curl -s -H "Authorization: Bearer $COMPUTER_API_TOKEN" \
  "http://localhost:9000/v1/sessions/$SID/screenshot" | jq -r .image | head -c 64
echo …

curl -s -H "Authorization: Bearer $COMPUTER_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"type":"key","text":"ctrl+l"}' \
  "http://localhost:9000/v1/sessions/$SID/action"

# Soft release (park warm) — or add ?recycle=true to wipe and free
curl -s -H "Authorization: Bearer $COMPUTER_API_TOKEN" \
  -X DELETE "http://localhost:9000/v1/sessions/$SID"
```

- Gateway: `http://localhost:9000` ([OpenAPI](http://localhost:9000/docs))
- Desktop Watch: lease `vnc_url` (noVNC on `:6081` / `:6082`)

Default inventory is two warm desktop seats (`gateway/workers.json`).

## Bind your agent

Tool shapes and full curl / Python examples: **[BINDING.md](BINDING.md)**  
OpenAI / Anthropic-style JSON: [`schemas/`](schemas/)

Loop: `POST /v1/sessions` → screenshot → model → `POST …/action` → `DELETE /v1/sessions/{id}`

## Docs for contributors / ops

Repo layout, seat lifecycle (lease / park / recycle), env vars, reverse-proxy notes: **[AGENTS.md](AGENTS.md)**

## Advanced profiles

Not required for the desktop golden path. How to enable each profile and inventory file: **[docs/profiles.md](docs/profiles.md)**

| Profile | What |
|---------|------|
| **Physical phone** | ADB seat + scrcpy Watch (`COMPOSE_PROFILES=phone`) |
| **Cloud phone** | Vendor session via `CloudPhoneProvider` (`phone-cloud`) |
| **k3s burst** | On-demand seats when reserved inventory is full |

Phone without `seat_id` leases **cloud** seats only; pin a physical device with `"seat_id"` after register/bind.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE)
