/**
 * `@computer-with-agent/client` — typed HTTP client for Computer Runtime `/v1`.
 *
 * Inference stays outside — this package only talks HTTP.
 */

export type {
  ComputerActionPayload,
  ComputerActionParams,
  ComputerActionType,
  ComputerEngineAction,
  ComputerLeaseInfo,
  ComputerLeaseParams,
  ComputerObserveElement,
  ComputerObserveResult,
  ComputerResult,
  ComputerSeatKind,
  GatewaySeat,
} from "./types.js";

export {
  ComputerEngine,
  type ComputerEngineOptions,
} from "./computer-engine.js";

export {
  claimPhoneSeat,
  type ClaimPhoneSeatOptions,
} from "./claim-phone-seat.js";

export * from "./tasks/index.js";
