/**
 * Whether an error is a code chunk that failed to download, as opposed to a
 * component that threw while rendering.
 *
 * The usual cause is a deploy: a tab opened before it asks for chunk hashes
 * the new build no longer serves. Only a reload fixes that, and another screen
 * may need a chunk of its own, so the answer differs from a render failure.
 * Webpack names the error `ChunkLoadError`; Turbopack and the browsers' own
 * dynamic-import failures are recognised by their messages.
 */
const CHUNK_MESSAGES =
  /failed to load chunk|loading chunk .* failed|failed to fetch dynamically imported module|error loading dynamically imported module|importing a module script failed/i;

export function isChunkLoadError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  return error.name === "ChunkLoadError" || CHUNK_MESSAGES.test(error.message);
}
