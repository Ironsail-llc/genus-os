import { ListSkeleton } from "@/components/business/list-skeleton";

export default function Loading() {
  return (
    <main className="min-h-svh bg-background p-4">
      <ListSkeleton testId="route-loading" label="Loading the Helm" className="mx-auto max-w-3xl" />
    </main>
  );
}
