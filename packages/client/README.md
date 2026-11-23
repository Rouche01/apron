# `@computer-with-agent/client`

Typed HTTP client for the [Computer Runtime](../..) gateway: lease / screenshot / observe / action / park / recycle.

- **Contract:** [`docs/v1.md`](../../docs/v1.md) · [`BINDING.md`](../../BINDING.md)
- **Inference:** out of scope — bring your own model/agent loop
- **Deps:** none on `@relay/*`; Node 18+ `fetch` (injectable)

## Quick use

```ts
import { ComputerEngine } from "@computer-with-agent/client";

const engine = new ComputerEngine({
  baseUrl: process.env.COMPUTER_GATEWAY_URL!,
  token: process.env.COMPUTER_API_TOKEN,
});

await engine.execute({ type: "lease", params: {} });
const shot = await engine.execute({ type: "screenshot", params: {} });
await engine.execute({ type: "release", params: {} }); // soft park
// or: await engine.execute({ type: "recycle", params: {} });
```

Phone: `params: { kind: "phone" }` on lease, or `claimPhoneSeat(engine)`. Desktop helpers: `ComputerTaskRunner` + `open_url` / `capture_frame` / `tour_urls`.

## Develop

```bash
cd packages/client && npm install && npm run build
```

## License

Apache-2.0 (same as the Computer Runtime repo).
