import {
  Activity,
  Bot,
  Brain,
  FileText,
  Flag,
  HeartPulse,
  Inbox,
  KeyRound,
  LayoutDashboard,
  ListTodo,
  MessageSquare,
  Palette,
  Plug,
  Puzzle,
  Radio,
  ScrollText,
  SlidersHorizontal,
  Store,
  UserCog,
  Users,
  Workflow,
  type LucideIcon,
} from "lucide-react";

/**
 * One description of the Helm's navigation. The sidebar, the mobile "More"
 * sheet and the ⌘K palette all read this, so the three can never drift.
 */

export type ViewId =
  | "chat"
  | "inbox"
  | "tasks"
  | "agents"
  | "workflows"
  | "dashboard"
  | "marketplace"
  | "runs"
  | "fleet"
  | "memory"
  | "audit"
  | "logs"
  | "health"
  | "settings";

export type SettingsPageId =
  | "providers"
  | "channels"
  | "users"
  | "secrets"
  | "config"
  | "flags"
  | "plugins"
  | "appearance";

export interface NavItem {
  /** Stable key — also the `nav-<id>` test id and the palette command id. */
  id: string;
  label: string;
  view: ViewId;
  /** Only set for Settings entries, which are sub-pages of one container view. */
  sub?: SettingsPageId;
  icon: LucideIcon;
  /** The view behind this item does not exist yet: render disabled, never a dead link. */
  soon?: boolean;
  /**
   * UX gate for ONE item, for the groups that are not gated wholesale.
   *
   * Observe is a mixed group: Runs, Fleet and Health are everybody's, Memory
   * and Logs are the operator's, and Audit is the operator's plus the
   * read-only `auditor`. Hiding the whole group to protect three of its items
   * would take the other three away from everyone, so the gate is per item.
   *
   * Like `NavGroup.requiresOperator`, this authorizes NOTHING — the bridge
   * checks the caller's role on every one of these routes, and a member who
   * types `?v=memory` gets a refusal from the server, not from this file.
   */
  requires?: "operator" | "audit";
}

export interface NavGroup {
  id: string;
  label: string;
  items: NavItem[];
  /**
   * UX gate only. Hiding a group removes it from every nav surface; it does NOT
   * authorize anything. Server-side authorization for these screens is a
   * separate task — the bridge still checks the caller's role on every route.
   */
  requiresOperator?: boolean;
}

/** Mirrors crm/bridge/routers/controls.py::OPERATOR_ROLES. */
const OPERATOR_ROLES = new Set(["owner", "admin"]);

export function isOperatorRole(role: string | null | undefined): boolean {
  return OPERATOR_ROLES.has(role ?? "");
}

/**
 * Mirrors `crm/bridge/routers/_operator.py::AUDIT_ROLES` — the operator roles
 * plus `auditor`.
 *
 * `auditor` reads the record of what was done and who widened which guardrail
 * (`GET /api/audit/*`, `GET /api/controls/audit`) and nothing else. It is
 * deliberately NOT an operator: Memory and Logs stay shut to it, because the
 * journal carries every value every process printed — a far wider surface than
 * a record of decisions — and a memory forget is a write.
 */
const AUDIT_ROLES = new Set([...OPERATOR_ROLES, "auditor"]);

export function isAuditReaderRole(role: string | null | undefined): boolean {
  return AUDIT_ROLES.has(role ?? "");
}

/** Whether `role` may see one nav item. UX only — see `NavItem.requires`. */
export function isNavItemVisible(item: NavItem, role: string | null | undefined): boolean {
  if (item.requires === "operator") return isOperatorRole(role);
  if (item.requires === "audit") return isAuditReaderRole(role);
  return true;
}

export interface SettingsPage {
  id: SettingsPageId;
  label: string;
  /** What will live here once the page is filled in by a later task. */
  description: string;
  group: string;
  icon: LucideIcon;
}

export const settingsPages: SettingsPage[] = [
  {
    id: "providers",
    label: "Providers",
    group: "Connections",
    icon: Plug,
    description:
      "Model providers and API keys — which vendor serves each tier, the fallback order, and the spend guardrails around them.",
  },
  {
    id: "channels",
    label: "Channels",
    group: "Connections",
    icon: Radio,
    description:
      "Where the operator and the agents talk: Telegram, email, the web chat, and the delivery policy for each of them.",
  },
  {
    id: "users",
    label: "Users & roles",
    group: "Access",
    icon: UserCog,
    description:
      "People with access to this instance, the role each one holds, and the invitations still outstanding.",
  },
  {
    id: "secrets",
    label: "Secrets",
    group: "Access",
    icon: KeyRound,
    description:
      "The encrypted secret store — which secrets exist, when each was last rotated, and which service reads it. Values are never shown here.",
  },
  {
    id: "config",
    label: "Config",
    group: "Platform",
    icon: SlidersHorizontal,
    description:
      "Instance configuration: workspace paths, tenant defaults, schedules, and the engine limits that shape every run.",
  },
  {
    id: "flags",
    label: "Flags",
    group: "Platform",
    icon: Flag,
    description:
      "Platform controls and their rollout state — off, observe, or enforce — with the reason recorded for each change.",
  },
  {
    id: "plugins",
    label: "Plugins",
    group: "Platform",
    icon: Puzzle,
    description:
      "Installed plugins and the tools each one contributes, with the version and source of every package.",
  },
  {
    id: "appearance",
    label: "Appearance",
    group: "Personal",
    icon: Palette,
    description:
      "Theme and density for this browser. These are personal preferences, not instance settings.",
  },
];

export const navGroups: NavGroup[] = [
  {
    id: "chat",
    label: "Chat",
    items: [{ id: "chat", label: "Chat", view: "chat", icon: MessageSquare }],
  },
  {
    id: "workspace",
    label: "Workspace",
    items: [
      { id: "inbox", label: "Inbox", view: "inbox", icon: Inbox },
      // Tasks is not in the operator's Workspace list; it is here because the
      // existing tasks view must stay reachable. Inbox is its successor but
      // does not carry review tasks yet — the approvals route behind it lists
      // workflow approvals and agent questions only — so this entry stays
      // until those move across.
      { id: "tasks", label: "Tasks", view: "tasks", icon: ListTodo },
      { id: "agents", label: "Agents", view: "agents", icon: Bot },
      // "Automations" is the product name for the existing workflows view.
      { id: "workflows", label: "Automations", view: "workflows", icon: Workflow },
      { id: "dashboard", label: "Dashboard", view: "dashboard", icon: LayoutDashboard },
      { id: "marketplace", label: "Marketplace", view: "marketplace", icon: Store },
    ],
  },
  {
    id: "observe",
    label: "Observe",
    items: [
      { id: "runs", label: "Runs", view: "runs", icon: Activity },
      { id: "fleet", label: "Fleet", view: "fleet", icon: Users },
      { id: "memory", label: "Memory", view: "memory", icon: Brain, requires: "operator" },
      { id: "audit", label: "Audit", view: "audit", icon: ScrollText, requires: "audit" },
      { id: "logs", label: "Logs", view: "logs", icon: FileText, requires: "operator" },
      { id: "health", label: "Health", view: "health", icon: HeartPulse },
    ],
  },
  {
    id: "settings",
    label: "Settings",
    requiresOperator: true,
    items: settingsPages.map((page) => ({
      id: `settings-${page.id}`,
      label: page.label,
      view: "settings" as ViewId,
      sub: page.id,
      icon: page.icon,
    })),
  },
];

/**
 * Nav groups a holder of `role` may see, with the items they may see inside
 * them. UX only — see `NavGroup.requiresOperator` and `NavItem.requires`.
 *
 * A group left with no items is dropped rather than rendered as a heading over
 * nothing. The sidebar, the mobile More sheet and the ⌘K palette all read this
 * one function, so none of them can disagree about who sees what.
 */
export function visibleNavGroups(role: string | null | undefined): NavGroup[] {
  const operator = isOperatorRole(role);
  return navGroups
    .filter((group) => !group.requiresOperator || operator)
    .map((group) => ({ ...group, items: group.items.filter((item) => isNavItemVisible(item, role)) }))
    .filter((group) => group.items.length > 0);
}

export const viewTitles: Record<ViewId, string> = {
  chat: "Chat",
  inbox: "Inbox",
  tasks: "Tasks",
  agents: "Agents",
  workflows: "Automations",
  dashboard: "Dashboard",
  marketplace: "Marketplace",
  runs: "Runs",
  fleet: "Fleet",
  memory: "Memory",
  audit: "Audit",
  logs: "Logs",
  health: "Health",
  settings: "Settings",
};

/**
 * Views that have no screen behind them yet.
 *
 * Empty as of the Observe pages — Memory, Audit and Logs are built. The set
 * and the `soon` machinery around it stay: the next view to be named in the
 * nav before it exists gets a disabled item with a pill rather than a dead
 * link, which is the whole reason this exists.
 */
const COMING_SOON_VIEWS = new Set<ViewId>();

export function isComingSoonView(view: ViewId): boolean {
  return COMING_SOON_VIEWS.has(view);
}

export function viewGroupLabel(view: ViewId): string {
  return navGroups.find((g) => g.items.some((i) => i.view === view))?.label ?? "";
}

export const ALL_VIEW_IDS = Object.keys(viewTitles) as ViewId[];
