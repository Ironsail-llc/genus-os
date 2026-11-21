/**
 * The rooms Bridge API's shapes, as the shared-threads contract writes them
 * (`docs/superpowers/specs/2026-11-21-shared-threads-design.md`, Contract →
 * Bridge API). JSON is camelCase, like the rest of the bridge.
 *
 * Code says "room"; the dashboard says "workspace" (`labels.ts`).
 */

export type Access = "viewer" | "shared" | "assigned" | "admin";
export type RoomKind = "report" | "agent" | "task";

export interface EditionHeadline {
  id: string;
  seq: number;
  title: string;
  periodStart: string | null;
  periodEnd: string | null;
}

export interface RoomSummary {
  id: string;
  kind: RoomKind;
  title: string;
  agentId: string;
  paused: boolean;
  archived: boolean;
  unread: number;
  lastActivityAt: string | null;
  latestEdition?: EditionHeadline;
}

export interface RoomMember {
  userId: string;
  displayName: string;
  assigned: boolean;
}

export interface Room extends RoomSummary {
  instructions: string;
  settings: Record<string, unknown>;
  rev: number;
  access: Access;
  runsAs?: { userId: string; displayName: string };
  members: RoomMember[];
  nextRunAt?: string | null;
}

export interface EditionBlock {
  component: string;
  props: Record<string, unknown>;
}

export interface EditionSuggestion {
  key: string;
  title: string;
  detail?: string;
  action_hint?: string;
}

export interface Edition {
  id: string;
  seq: number;
  kind: "edition" | "dashboard";
  status: "building" | "published" | "failed";
  preview: boolean;
  periodStart: string | null;
  periodEnd: string | null;
  grain: "day" | "week" | "month" | "custom" | null;
  revision: number;
  title: string;
  summary: string;
  blocks: EditionBlock[];
  suggestions: EditionSuggestion[];
  provenance: Record<string, unknown> | null;
  createdAt: string;
}
