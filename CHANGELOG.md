# Changelog

## Unreleased

- Hosted beta ops: [docs/hosted-ops.md](docs/hosted-ops.md), [`scripts/hosted-health-probe.sh`](scripts/hosted-health-probe.sh), [`scripts/recording-backup.sh`](scripts/recording-backup.sh)

## [v0.1.0] — OSS launch

First public-oriented tag of **Computer Runtime**.

- Desktop Compose golden path (gateway + two warm seats)
- Stable `/v1` lease / park / recycle / screenshot / action contract ([docs/v1.md](docs/v1.md))
- Opt-in session recording journals (`record: true` / `RECORD_SESSIONS_DEFAULT`) with `GET /v1/recordings`
- CI: gateway pool, provider, and recording unit tests
- Smoke: [`scripts/oss-smoke.sh`](scripts/oss-smoke.sh) · [docs/oss-smoke.md](docs/oss-smoke.md)

Phone / cloud / k3s burst remain advanced profiles ([docs/profiles.md](docs/profiles.md)).
