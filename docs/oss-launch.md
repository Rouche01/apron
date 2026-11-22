# OSS public launch hygiene

Closes the productize **OSS public launch** gate (`github-public`). Pair with [oss-smoke.md](oss-smoke.md).

## Must not be in git

| Keep local only | Notes |
|-----------------|--------|
| `.env` | Ignored; copy from `.env.example` |
| Real `COMPUTER_API_TOKEN` / `PHONE_HARNESS_API_KEY` | Placeholders only in examples (`dev-token`, `pck_…`) |
| kubeconfigs, TLS keys, phone unlock secrets | Never commit |
| `.cursor/`, `.data/`, recording volume contents | Ignored |

Before every public push:

```bash
git status
git check-ignore -v .env
# Fail the launch if any real key appears in tracked files:
git grep -E 'pck_[a-zA-Z0-9]{20,}' -- ':!.cursor' || true
```

`.env.example` and `gateway/.env.example` may use **`dev-token`** and commented `pck_…` only — never a live harness key.

## Tag

Annotated tags mark OSS releases strangers can pin:

```bash
git tag -a v0.1.0 -m "OSS launch: desktop Compose golden path + session recordings"
git push origin main
git push origin v0.1.0
```

Then set the GitHub repo to **public** (Settings → General → Danger zone) if it is still private. Enable [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/working-with-repository-security-advisories/configuring-private-vulnerability-reporting-for-a-repository) so [SECURITY.md](../SECURITY.md) works.

## After launch

- Confirm CI is green on the tagged commit.
- Optional: redeploy ThinkPad / Caddy with the same tag.
- Organic distribution: [organic GTM plan](../.cursor/plans/organic_gtm_oss.plan.md) (not required for the tag itself).
