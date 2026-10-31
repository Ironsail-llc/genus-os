import Link from "next/link";

export default function NotFound() {
  return (
    <main
      data-testid="not-found"
      className="flex min-h-svh flex-col items-center justify-center gap-3 bg-background p-4 text-center"
    >
      <h1 className="text-sm font-medium text-foreground">This page does not exist</h1>
      <p className="max-w-sm text-xs text-muted-foreground">
        The address may be from an older version of the dashboard. Every view lives on the Helm.
      </p>
      <Link
        href="/"
        className="rounded-md border border-border px-3 py-1.5 text-xs text-foreground hover:bg-accent"
      >
        Back to the Helm
      </Link>
    </main>
  );
}
