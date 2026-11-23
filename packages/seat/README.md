# `@useapron/seat`

Seat policy loop for Apron: lease → wait → observe → decide → act → park.

| | |
|--|--|
| **Depends on** | [`@useapron/client`](../client) (`^0.1.1`) |
| **Inference** | Out of scope — inject a `SeatDecider` (LLM or scripted) |
| **Contract** | [`docs/v1.md`](../../docs/v1.md) · [`BINDING.md`](../../BINDING.md) §7 |
| **License** | Apache-2.0 |

## Layers

| Package | Role |
|---------|------|
| `@useapron/client` | Gateway HTTP — lease / screenshot / observe / action / park |
| `@useapron/seat` | Shared loop — timing, stuck guards, `runSeatPolicyLoop` |
| Your agent | `decide()` — BYO vision model or heuristics |

Curl / raw HTTP still work without either package ([BINDING](../../BINDING.md) §2).

## Install

```bash
npm install @useapron/client @useapron/seat
```

From a sibling checkout (unpublished):

```bash
npm install ../computer-with-agent/packages/client ../computer-with-agent/packages/seat
```

## Quick use (inject `decide`)

```ts
import { ComputerEngine } from "@useapron/client";
import {
  captureScreenshotObservation,
  defaultSeatTiming,
  runSeatPolicyLoop,
  type SeatDecideInput,
  type SeatDecision,
} from "@useapron/seat";

const engine = new ComputerEngine({
  baseUrl: process.env.COMPUTER_GATEWAY_URL ?? "http://localhost:9000",
  token: process.env.COMPUTER_API_TOKEN,
});

async function decide(input: SeatDecideInput): Promise<SeatDecision> {
  // Pass input.observation.imageBase64 (+ optional elements) to your model.
  // return { kind: "act", action: { type: "click", x: 100, y: 200 } };
  // return { kind: "need_human", reason: "CAPTCHA" };
  void input;
  return { kind: "done", reason: "stub" };
}

const result = await runSeatPolicyLoop(engine, {
  goal: { text: "Find the pricing page" },
  timing: defaultSeatTiming({ exitRateMu: 0 }),
  maxIterations: 12,
  decide: { decide },
  captureObservation: ({ iteration }) =>
    captureScreenshotObservation(engine, iteration),
});

console.log(result.stoppedReason, result.steps.length, result.vncUrl);
```

### Phone

```ts
import { captureObserveObservation } from "@useapron/seat";

await runSeatPolicyLoop(engine, {
  goal: { text: "Open Settings → About phone" },
  leaseKind: "phone",
  timing: defaultSeatTiming({ exitRateMu: 0 }),
  decide: { decide },
  captureObservation: ({ iteration }) =>
    captureObserveObservation(engine, iteration),
  maxSameActionStreak: 3,
  maxSameScreenStreak: 6,
});
```

## Develop

```bash
cd packages/seat
npm install
npm test
npm run build
```

## Release (CI)

Publishes from GitHub Actions on a version tag via **npm Trusted Publisher** (OIDC) — no long-lived `NPM_TOKEN`. Not on every `main` push.

### One-time npm setup

On [npm package settings](https://www.npmjs.com/package/@useapron/seat) → **Trusted Publisher** → GitHub Actions (create the empty scoped package under `useapron` first if needed):

| Field | Value |
|-------|--------|
| Organization or user | `Rouche01` |
| Repository | `computer-with-agent` |
| Workflow filename | `publish-seat.yml` (filename only) |

Allow direct `npm publish` if the UI offers staged-only vs publish.

### Each release

1. Bump `"version"` in `package.json` (currently `0.1.0`)
2. Merge to the default branch
3. Tag and push (must match the version):

```bash
git tag seat-v0.1.0
git push origin seat-v0.1.0
```

Push the tag **once**. Re-running the workflow or re-pushing the same tag will no-op if that version is already on npm (workflow skips). Do not force-recreate `seat-v*` tags after a successful publish.

Workflow: [`.github/workflows/publish-seat.yml`](../../.github/workflows/publish-seat.yml).
