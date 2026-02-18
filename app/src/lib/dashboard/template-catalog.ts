/**
 * Machine-readable catalog of all available business components.
 * Used by the dashboard subagent system prompt.
 */

export interface TemplateEntry {
  name: string;
  description: string;
  category: "data" | "chart" | "layout" | "input";
  propsInterface: string;
  example: string;
}

export const TEMPLATE_CATALOG: TemplateEntry[] = [
  {
    name: "ContactCard",
    description: "Display a single contact with avatar, name, title, company, email",
    category: "data",
    propsInterface: `{ name: { firstName: string; lastName: string }; email?: string; jobTitle?: string; company?: { name: string } | null; phone?: string; avatarUrl?: string }`,
    example: `<ContactCard name={{ firstName: "John", lastName: "Doe" }} email="john@example.com" jobTitle="CTO" />`,
  },
  {
    name: "ContactTable",
    description: "Sortable table of contacts with name, email, job title, company columns",
    category: "data",
    propsInterface: `{ data: Array<{ id: string; name: { firstName: string; lastName: string }; email?: string; jobTitle?: string; company?: { name: string } | null }> }`,
    example: `<ContactTable data={contacts} />`,
  },
  {
    name: "CompanyCard",
    description: "Display a company with domain, employees, revenue",
    category: "data",
    propsInterface: `{ name: string; domainName?: string; employees?: number; annualRecurringRevenue?: number; address?: string }`,
    example: `<CompanyCard name="Acme Inc" domainName="acme.com" employees={50} />`,
  },
  {
    name: "ConversationList",
    description: "List of conversations with contact name, status, message count",
    category: "data",
    propsInterface: `{ conversations: Array<{ id: number; status: string; contact: { name: string }; messages_count: number; unread_count: number; last_activity_at: string }> }`,
    example: `<ConversationList conversations={conversations} />`,
  },
  {
    name: "ConversationThread",
    description: "Message thread for a single conversation",
    category: "data",
    propsInterface: `{ messages: Array<{ id: number; content: string; message_type: "incoming" | "outgoing"; sender?: { name: string }; created_at: string }> }`,
    example: `<ConversationThread messages={messages} />`,
  },
  {
    name: "MetricCard",
    description: "Single metric display with label, value, optional trend",
    category: "data",
    propsInterface: `{ label: string; value: string | number; description?: string; trend?: "up" | "down" | "flat"; trendValue?: string }`,
    example: `<MetricCard label="Active Contacts" value={142} trend="up" trendValue="+12 this week" />`,
  },
  {
    name: "MetricGrid",
    description: "Grid of MetricCards",
    category: "layout",
    propsInterface: `{ metrics: Array<{ label: string; value: string | number; description?: string; trend?: "up" | "down" | "flat"; trendValue?: string }> }`,
    example: `<MetricGrid metrics={[{ label: "Contacts", value: 142 }, { label: "Open Convos", value: 5 }]} />`,
  },
  {
    name: "BarChart",
    description: "Bar chart using Recharts",
    category: "chart",
    propsInterface: `{ data: Array<Record<string, string | number>>; xKey: string; yKey: string; title?: string }`,
    example: `<BarChart data={[{month:"Jan",count:10},{month:"Feb",count:20}]} xKey="month" yKey="count" title="Monthly Activity" />`,
  },
  {
    name: "LineChart",
    description: "Line chart using Recharts",
    category: "chart",
    propsInterface: `{ data: Array<Record<string, string | number>>; xKey: string; yKey: string; title?: string }`,
    example: `<LineChart data={[{day:"Mon",value:5},{day:"Tue",value:8}]} xKey="day" yKey="value" />`,
  },
  {
    name: "PieChart",
    description: "Pie chart using Recharts",
    category: "chart",
    propsInterface: `{ data: Array<{ name: string; value: number }>; title?: string }`,
    example: `<PieChart data={[{name:"Open",value:5},{name:"Resolved",value:12}]} title="Conversation Status" />`,
  },
  {
    name: "DataTable",
    description: "Generic sortable data table (TanStack Table)",
    category: "data",
    propsInterface: `{ columns: Array<{ key: string; label: string }>; data: Array<Record<string, unknown>> }`,
    example: `<DataTable columns={[{key:"name",label:"Name"},{key:"value",label:"Value"}]} data={rows} />`,
  },
  {
    name: "Timeline",
    description: "Vertical timeline of events",
    category: "data",
    propsInterface: `{ events: Array<{ title: string; description?: string; timestamp: string; type?: string }> }`,
    example: `<Timeline events={[{title:"Email received",timestamp:"2026-02-18T10:00:00Z"}]} />`,
  },
  {
    name: "MemorySearch",
    description: "Display memory search results with similarity scores",
    category: "data",
    propsInterface: `{ results: Array<{ content: string; category: string; similarity: number; created_at: string }>; query: string }`,
    example: `<MemorySearch results={results} query="recent meetings" />`,
  },
  {
    name: "ServiceHealth",
    description: "Service health status grid with colored indicators",
    category: "data",
    propsInterface: `{ services: Array<{ name: string; url: string; status: "healthy" | "unhealthy"; responseTime?: number }>; overallStatus: "ok" | "degraded" }`,
    example: `<ServiceHealth services={services} overallStatus="ok" />`,
  },
  {
    name: "TaskBoard",
    description: "Kanban-style task board with columns",
    category: "data",
    propsInterface: `{ tasks: Array<{ id: string; title: string; status: string; assignee?: string }> }`,
    example: `<TaskBoard tasks={tasks} />`,
  },
  {
    name: "MarkdownView",
    description: "Render markdown content",
    category: "layout",
    propsInterface: `{ content: string; title?: string }`,
    example: `<MarkdownView content="# Hello\\nSome **bold** text" title="Notes" />`,
  },
  {
    name: "FormPanel",
    description: "Dynamic form with field definitions",
    category: "input",
    propsInterface: `{ title: string; fields: Array<{ name: string; label: string; type: "text" | "email" | "number" | "select"; required?: boolean; options?: string[] }>; onSubmit?: (data: Record<string, unknown>) => void }`,
    example: `<FormPanel title="New Contact" fields={[{name:"firstName",label:"First Name",type:"text",required:true}]} />`,
  },
];

/** Generate the component catalog section for the dashboard system prompt */
export function generateCatalogPrompt(): string {
  const sections = TEMPLATE_CATALOG.map(
    (t) =>
      `### ${t.name} (${t.category})
${t.description}
Props: ${t.propsInterface}
Example: ${t.example}`
  );

  return `## Available Components

Import these from "@/components/business":
\`\`\`tsx
import { ${TEMPLATE_CATALOG.map((t) => t.name).join(", ")} } from "@/components/business";
\`\`\`

${sections.join("\n\n")}`;
}
