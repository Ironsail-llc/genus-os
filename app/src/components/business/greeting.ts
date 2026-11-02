import { useSyncExternalStore } from "react";

export function computeGreeting(): string {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}

export function formatToday(): string {
  return new Date().toLocaleDateString("en-US", {
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

// `useSyncExternalStore` with constant snapshots is the React-19-blessed way
// to render different output on server vs client without a hydration mismatch
// and without a setState-in-useEffect that trips react-hooks linting and
// pollutes test runs. The server snapshot is `false`, the client snapshot is
// `true`, and the subscribe fn is a no-op (the value never changes after
// mount).
const subscribe = () => () => {};
export function useIsClient(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => true,
    () => false,
  );
}
