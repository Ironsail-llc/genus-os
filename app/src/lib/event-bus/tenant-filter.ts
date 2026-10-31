/**
 * Which tenant an event belongs to, and which events a session may see.
 *
 * Every engine cell and the bridge publish to the same Redis streams, each
 * event tagged with the tenant it happened in. The engines read only their
 * own tenant's events (robothor/events/bus.py); the dashboard applies the
 * same rule for the session it serves, so a child tenant's admin never sees
 * the root's or a sibling's events. An untagged event is the root tenant's,
 * the engines' rule too.
 */
import type { EventEnvelope } from "@/lib/event-bus/redis-client";
import { rootTenant } from "@/lib/services/registry";

/** The tenant an event belongs to; untagged means the root tenant. */
export function eventTenant(event: Pick<EventEnvelope, "tenant_id">): string {
  return event.tenant_id?.trim() || rootTenant();
}

/** The events a session acting in `tenant` may see. No tenant, nothing. */
export function visibleToTenant<T extends Pick<EventEnvelope, "tenant_id">>(
  events: T[],
  tenant: string | undefined | null,
): T[] {
  if (!tenant) return [];
  return events.filter((event) => eventTenant(event) === tenant);
}
