# `@useapron/client`

Typed HTTP client for the [Computer Runtime](../..) gateway: lease / screenshot / observe / action / park / recycle.

| | |
|--|--|
| **Contract** | [`docs/v1.md`](../../docs/v1.md) · [`BINDING.md`](../../BINDING.md) §6 |
| **Inference** | Out of scope — bring your own model |
| **Deps** | None on `@relay/*`; Node 18+ `fetch` (injectable) |
| **License** | Apache-2.0 |

## Compatibility

| Client | Gateway `/v1` |
|--------|----------------|
| `0.1.x` | OpenAPI **0.3.x** (paths/semantics in `docs/v1.md`) |

Bump the client **major** when guaranteed `/v1` behavior breaks (new API prefix or documented deprecation). Additive gateway fields are fine without a client major.

## Install

```bash
npm install @useapron/client
```

From a sibling checkout (dev / unpublished changes):

```bash
npm install ../computer-with-agent/packages/client
# build once: cd …/packages/client && npm install && npm run build
```

Env (same as curl bind):

```bash
export COMPUTER_GATEWAY_URL=http://localhost:9000
export COMPUTER_API_TOKEN=dev-token
```

## Desktop lease loop

```ts
import { ComputerEngine } from "@useapron/client";

const engine = new ComputerEngine({
  baseUrl: process.env.COMPUTER_GATEWAY_URL!,
  token: process.env.COMPUTER_API_TOKEN,
  agentId: "demo",
});

const lease = await engine.execute({
  type: "lease",
  params: { job_id: "demo-job", ttl_sec: 600 },
});
if (!lease.success) throw new Error(lease.error);

console.log("Watch:", engine.getVncUrl());

try {
  const shot = await engine.execute({ type: "screenshot", params: {} });
  // … model(shot.data?.image) …
  await engine.execute({
    type: "action",
    params: { action: { type: "key", text: "ctrl+l" } },
  });
} finally {
  // Soft park (warm): await engine.execute({ type: "release", params: {} });
  await engine.execute({ type: "recycle", params: {} });
}
```

Helpers: `ComputerTaskRunner` + builtins `open_url` / `capture_frame` / `tour_urls`, or `navigateToUrl(engine, url)`.

## Phone

```ts
await engine.execute({
  type: "lease",
  params: { kind: "phone", job_id: "phone-job" },
});
// or: import { claimPhoneSeat } from "@useapron/client";
// await claimPhoneSeat(engine);

const obs = await engine.execute({
  type: "observe",
  params: { max_elements: 100 },
});
```

Physical pin / cloud selection follows gateway rules in BINDING / `docs/v1.md` (`seat_id`, cloud-only default for bare `kind: "phone"`).

## Develop

```bash
cd packages/client
npm install
npm test
npm run build
```

## Release (CI)

Publishes from GitHub Actions on a version tag via **npm Trusted Publisher** (OIDC) — no long-lived `NPM_TOKEN`. Not on every `main` push.

### One-time npm setup

On [npm package settings](https://www.npmjs.com/package/@useapron/client) → **Trusted Publisher** → GitHub Actions:

| Field | Value |
|-------|--------|
| Organization or user | `Rouche01` |
| Repository | `computer-with-agent` |
| Workflow filename | `publish-client.yml` (filename only) |

Allow direct `npm publish` if the UI offers staged-only vs publish.

### Each release

1. Bump `"version"` in `package.json` (e.g. `0.1.1`)
2. Merge to the default branch
3. Tag and push (must match the version):

```bash
git tag client-v0.1.1
git push origin client-v0.1.1
```

Push the tag **once**. Re-running the workflow or re-pushing the same tag will no-op if that version is already on npm (workflow skips). Do not force-recreate `client-v*` tags after a successful publish.

Workflow: [`.github/workflows/publish-client.yml`](../../.github/workflows/publish-client.yml).

Semver: bump **major** when guaranteed [`docs/v1.md`](../../docs/v1.md) behavior breaks; otherwise patch/minor as usual. Client `0.1.x` tracks OpenAPI **0.3.x**.

Optional one-time cleanup of the org placeholder:

```bash
npm deprecate @useapron/client@0.0.0-stage "Use @useapron/client@0.1.0 or later"
```


Curl-only bind (no npm): [BINDING.md](../../BINDING.md) §2.
