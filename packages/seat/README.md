# `@useapron/seat`

Seat policy loop for Use Apron / Computer Runtime: lease → wait → observe → decide → act → park.

| | |
|--|--|
| **Depends on** | [`@useapron/client`](../client) |
| **Inference** | Out of scope — inject a `SeatDecider` (LLM or scripted) |
| **Contract** | [`docs/v1.md`](../../docs/v1.md) · [`BINDING.md`](../../BINDING.md) |
| **License** | Apache-2.0 |

## Install

```bash
npm install @useapron/seat
```

## Quick use

```ts
import { ComputerEngine } from "@useapron/client";
import {
  captureScreenshotObservation,
  defaultSeatTiming,
  runSeatPolicyLoop,
} from "@useapron/seat";

const engine = new ComputerEngine({
  baseUrl: process.env.COMPUTER_GATEWAY_URL!,
  token: process.env.COMPUTER_API_TOKEN,
});

const result = await runSeatPolicyLoop(engine, {
  goal: { text: "Open example.com and stop" },
  timing: defaultSeatTiming({ exitRateMu: 0 }),
  maxIterations: 10,
  decide: {
    async decide() {
      return { kind: "done", reason: "stub" };
    },
  },
  captureObservation: ({ iteration }) =>
    captureScreenshotObservation(engine, iteration),
});
```

Phone: `leaseKind: "phone"` + `captureObserveObservation`. Stuck guards: `maxSameActionStreak` / `maxSameScreenStreak`.

## Develop

```bash
cd packages/seat && npm install && npm run build
```
