# `@useapron/seat`

Seat policy loop for [Use Apron](../..) / Computer Runtime: lease → wait → observe → decide → act → park.

| | |
|--|--|
| **Depends on** | [`@useapron/client`](../client) |
| **Inference** | Out of scope — inject a `SeatDecider` (LLM or scripted) |
| **Contract** | [`docs/v1.md`](../../docs/v1.md) · [`BINDING.md`](../../BINDING.md) |
| **License** | Apache-2.0 |

## Status

Scaffold only. `runSeatPolicyLoop` and related APIs are ported next from relay-os `@relay/computer-seat-policy`.

## Install (when published)

```bash
npm install @useapron/seat
```

Develop from this repo:

```bash
cd packages/seat && npm install && npm run build
```

## Layers

| Package | Role |
|---------|------|
| `@useapron/client` | Gateway HTTP (lease / screenshot / action) |
| `@useapron/seat` | Shared loop chassis (this package) |
| Your agent / prompts | `decide()` — BYO model |
