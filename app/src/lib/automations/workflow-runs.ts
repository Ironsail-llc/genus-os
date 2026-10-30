// Stub for the RED phase: the shape the tests import, with no behaviour yet.
/* eslint-disable @typescript-eslint/no-unused-vars */
export function stepTally(_run: Record<string, number | null | undefined>): string | null {
  throw new Error("not implemented");
}
export function truncationOf(_value: unknown): { originalBytes: number; preview: string } | null {
  throw new Error("not implemented");
}
export function prettyJson(_value: unknown): string {
  throw new Error("not implemented");
}
export function runsUrl(_workflowId: string): string {
  throw new Error("not implemented");
}
export function runUrl(_workflowId: string, _runId: string): string {
  throw new Error("not implemented");
}
