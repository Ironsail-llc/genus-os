// The marketplace host behind the dashboard's Browse and per-agent links. It
// mirrors the CLI hub client's PROGRAMMATIC_RESOURCES_URL default so the two
// cannot drift apart; operators override it with
// NEXT_PUBLIC_PROGRAMMATIC_RESOURCES_URL.
export const DEFAULT_MARKETPLACE_URL = "https://programmaticresources.io";

export function marketplaceUrl(configured: string | undefined): string {
  const value = (configured ?? "").trim().replace(/\/+$/, "");
  return value || DEFAULT_MARKETPLACE_URL;
}
