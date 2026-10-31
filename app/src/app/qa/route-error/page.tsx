import { notFound } from "next/navigation";

export const dynamic = "force-dynamic";

/**
 * A route that throws on purpose, for the Playwright check of `error.tsx`.
 * It exists only when the server runs with GENUS_ENVIRONMENT=test; on every
 * other environment it is a 404.
 */
export default function ForcedRouteError() {
  if (process.env.GENUS_ENVIRONMENT !== "test") notFound();
  throw new Error("Forced route error for the error-page check");
}
