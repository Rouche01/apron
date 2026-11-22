# Security

## Reporting a vulnerability

Please **do not** file a public GitHub issue for security problems.

**Preferred:** use [GitHub private vulnerability reporting](https://github.com/Rouche01/computer-with-agent/security/advisories/new) on this repository (Security → Advisories → Report a vulnerability), if enabled.

**Otherwise:** email **richardemate@gmail.com** with:

- A short description of the issue and impact
- Steps to reproduce or a proof of concept (if safe to share)
- Affected components (gateway, desktop worker, phone worker, scrcpy-web, deploy)

We aim to acknowledge reports within a few business days.

## Scope notes

- Keep worker control ports (`:8000`) off the public internet; publish gateway + Watch (noVNC / scrcpy) via reverse proxy or localhost only — see [AGENTS.md](AGENTS.md).
- Bearer tokens (`COMPUTER_API_TOKEN`) and cloud phone API keys must not be committed.
