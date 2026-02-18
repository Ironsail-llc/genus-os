import type { ComponentType } from "react";
import {
  ContactCard,
  ContactTable,
  CompanyCard,
  ConversationList,
  ConversationThread,
  MetricCard,
  MetricGrid,
  BarChart,
  LineChart,
  PieChart,
  DataTable,
  Timeline,
  MemorySearch,
  ServiceHealth,
  TaskBoard,
  MarkdownView,
  FormPanel,
} from "@/components/business";

export interface ToolRegistration {
  name: string;
  description: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  component: ComponentType<any>;
}

const registry: Map<string, ToolRegistration> = new Map();

function register(
  name: string,
  description: string,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  component: ComponentType<any>
) {
  registry.set(name, { name, description, component });
}

// Register all business components
register("render_contact_card", "Display a contact card", ContactCard);
register("render_contact_table", "Display contacts in a table", ContactTable);
register("render_company_card", "Display a company card", CompanyCard);
register("render_conversations", "Display conversation list", ConversationList);
register("render_conversation_thread", "Display conversation messages", ConversationThread);
register("render_metric_card", "Display a single metric", MetricCard);
register("render_metric_grid", "Display a grid of metrics", MetricGrid);
register("render_bar_chart", "Display a bar chart", BarChart);
register("render_line_chart", "Display a line chart", LineChart);
register("render_pie_chart", "Display a pie chart", PieChart);
register("render_data_table", "Display a data table", DataTable);
register("render_timeline", "Display a timeline of events", Timeline);
register("render_memory_search", "Display memory search results", MemorySearch);
register("render_service_health", "Display service health status", ServiceHealth);
register("render_task_board", "Display tasks in a kanban board", TaskBoard);
register("render_markdown", "Display markdown content", MarkdownView);
register("render_form", "Display an input form", FormPanel);

export function getComponent(
  toolName: string
): ToolRegistration | undefined {
  return registry.get(toolName);
}

export function getAllTools(): ToolRegistration[] {
  return Array.from(registry.values());
}

export function hasComponent(toolName: string): boolean {
  return registry.has(toolName);
}
