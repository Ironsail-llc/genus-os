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
      { id: "inbox", label: "Inbox", view: "inbox", icon: Inbox, soon: true },
      // Tasks is not in the operator's Workspace list; it is here because the
      // existing tasks view must stay reachable. Inbox is its successor, so
      // this entry goes away when Inbox ships.
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
      { id: "memory", label: "Memory", view: "memory", icon: Brain, soon: true },
      { id: "audit", label: "Audit", view: "audit", icon: ScrollText, soon: true },
      { id: "logs", label: "Logs", view: "logs", icon: FileText, soon: true },
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

/** Nav groups a holder of `role` may see. UX only — see NavGroup.requiresOperator. */
export function visibleNavGroups(role: string | null | undefined): NavGroup[] {
  const operator = isOperatorRole(role);
  return navGroups.filter((group) => !group.requiresOperator || operator);
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

/** Views that have no screen behind them yet. */
const COMING_SOON_VIEWS = new Set<ViewId>(["inbox", "memory", "audit", "logs"]);

export function isComingSoonView(view: ViewId): boolean {
  return COMING_SOON_VIEWS.has(view);
}

export function viewGroupLabel(view: ViewId): string {
  return navGroups.find((g) => g.items.some((i) => i.view === view))?.label ?? "";
}

export const ALL_VIEW_IDS = Object.keys(viewTitles) as ViewId[];
