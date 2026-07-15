// The security core of the canvas bridge. Pure logic: no React, no network.
// Every read the sandboxed canvas can make and every action it can propose is
// declared here; anything not declared is dropped. The iframe names an OP, never
// a URL — this module is the only place op→path resolution happens.

// A flag id is UPPER_SNAKE with a ROBOTHOR_ prefix (matches the governed flags);
// an entity id (agent/run/workflow) is a conservative slug — no slashes, no dots,
// no traversal. These guards keep an iframe-supplied value from escaping its path.
const FLAG_NAME = /^ROBOTHOR_[A-Z0-9_]+$/;
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;

function safeId(v: unknown): string | null {
  return typeof v === "string" && SAFE_ID.test(v) ? v : null;
}

type ReadSpec = { path: (args: Record<string, unknown>) => string | null };

const READ_OPS: Record<string, ReadSpec> = {
  get_flags: { path: () => "/api/controls" },
  get_fleet: { path: () => "/api/fleet" },
  get_agent: { path: (a) => { const id = safeId(a.id); return id && `/api/fleet/${id}`; } },
  get_runs: { path: () => "/api/runs" },
  get_run: { path: (a) => { const id = safeId(a.id); return id && `/api/runs/${id}`; } },
  get_workflows: { path: () => "/api/workflows" },
  get_workflow_runs: { path: (a) => { const id = safeId(a.id); return id && `/api/workflows/${id}/runs`; } },
  get_health: { path: () => "/api/health/system" },
};

export type ReadOp = keyof typeof READ_OPS;

export function resolveReadOp(op: string, args: Record<string, unknown> = {}): { path: string } | null {
  const spec = READ_OPS[op];
  if (!spec) return null;
  const path = spec.path(args);
  return path ? { path } : null;
}

// The ONLY proposable action in Phase 3: set_flag → the Phase-1 operator flag PATCH.
// `describe` is built HERE from the resolved action, so the confirm dialog can never
// be worded by the iframe. Value legality (against the flag's value set) is enforced
// server-side by the controls PATCH (422); here we only shape/guard the request.
export function resolveProposeAction(
  action: string,
  args: Record<string, unknown>,
): { method: "PATCH"; path: string; body: { value: string; reason: string }; describe: string } | null {
  if (action !== "set_flag") return null;
  const name = typeof args.name === "string" && FLAG_NAME.test(args.name) ? args.name : null;
  const value = typeof args.value === "string" && args.value.length > 0 && args.value.length < 32 ? args.value : null;
  if (!name || !value) return null;
  const reason = typeof args.reason === "string" ? args.reason.slice(0, 500) : "proposed from canvas";
  return {
    method: "PATCH",
    path: `/api/controls/${name}`,
    body: { value, reason },
    describe: `Set ${name} → ${value}`,
  };
}

export type CanvasMessage =
  | { __robothor: true; kind: "read"; reqId: string; op: string; args?: Record<string, unknown> }
  | { __robothor: true; kind: "propose"; reqId: string; action: string; args: Record<string, unknown>; label?: string };

export function isCanvasMessage(data: unknown): data is CanvasMessage {
  if (typeof data !== "object" || data === null) return false;
  const d = data as Record<string, unknown>;
  if (d.__robothor !== true || typeof d.reqId !== "string") return false;
  if (d.kind === "read") return typeof d.op === "string";
  if (d.kind === "propose") return typeof d.action === "string" && typeof d.args === "object" && d.args !== null;
  return false;
}
