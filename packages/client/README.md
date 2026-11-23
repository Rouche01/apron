# `@computer-with-agent/client`

Typed HTTP client for the [Computer Runtime](../..) gateway: lease / screenshot / observe / action / park / recycle.

- **Contract:** [`docs/v1.md`](../../docs/v1.md) · [`BINDING.md`](../../BINDING.md)
- **Inference:** out of scope — bring your own model/agent loop
- **Deps:** none on `@relay/*`; Node 18+ `fetch` (injectable)

## Status

Scaffold only. `ComputerEngine` and related APIs are ported next from relay-os `@relay/engines-computer` (protocol-free).

## Install (when published)

```bash
npm install @computer-with-agent/client
```

Until publish, develop from this repo:

```bash
cd packages/client && npm install && npm run build
```

## License

Apache-2.0 (same as the Computer Runtime repo).
