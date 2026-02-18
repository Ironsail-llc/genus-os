import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

// Mock Recharts - it doesn't work in happy-dom
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="responsive-container">{children}</div>
  ),
  BarChart: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="recharts-bar">{children}</div>
  ),
  Bar: () => null,
  LineChart: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="recharts-line">{children}</div>
  ),
  Line: () => null,
  PieChart: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="recharts-pie">{children}</div>
  ),
  Pie: () => null,
  Cell: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  Legend: () => null,
}));

import { ContactCard } from "@/components/business/contact-card";
import { ContactTable } from "@/components/business/contact-table";
import { ConversationList } from "@/components/business/conversation-list";
import { ConversationThread } from "@/components/business/conversation-thread";
import { MetricCard } from "@/components/business/metric-card";
import { MetricGrid } from "@/components/business/metric-grid";
import { BarChart } from "@/components/business/bar-chart";
import { LineChart } from "@/components/business/line-chart";
import { PieChart } from "@/components/business/pie-chart";
import { Timeline } from "@/components/business/timeline";
import { MemorySearch } from "@/components/business/memory-search";
import { ServiceHealth } from "@/components/business/service-health";
import { TaskBoard } from "@/components/business/task-board";
import { MarkdownView } from "@/components/business/markdown-view";
import { FormPanel } from "@/components/business/form-panel";
import type { Person, Conversation, Message, MemorySearchResult } from "@/lib/api/types";

const mockPerson: Person = {
  id: "1",
  name: { firstName: "John", lastName: "Doe" },
  email: "john@example.com",
  jobTitle: "Engineer",
  company: { name: "Acme Inc" },
  city: "New York",
};

describe("ContactCard", () => {
  it("renders person name, email, job title", () => {
    render(<ContactCard person={mockPerson} />);
    expect(screen.getByText("John Doe")).toBeInTheDocument();
    expect(screen.getByText("john@example.com")).toBeInTheDocument();
    expect(screen.getByText("Engineer")).toBeInTheDocument();
  });

  it("shows loading skeleton", () => {
    render(<ContactCard loading />);
    expect(screen.getByTestId("contact-card-skeleton")).toBeInTheDocument();
  });

  it("handles missing optional fields", () => {
    const minimal: Person = {
      id: "2",
      name: { firstName: "Jane", lastName: "" },
    };
    render(<ContactCard person={minimal} />);
    expect(screen.getByText("Jane")).toBeInTheDocument();
  });

  it("clicking calls onSelect", () => {
    const onSelect = vi.fn();
    render(<ContactCard person={mockPerson} onSelect={onSelect} />);
    fireEvent.click(screen.getByTestId("contact-card"));
    expect(onSelect).toHaveBeenCalledWith(mockPerson);
  });
});

describe("ContactTable", () => {
  const people: Person[] = [
    mockPerson,
    {
      id: "2",
      name: { firstName: "Jane", lastName: "Smith" },
      email: "jane@example.com",
    },
  ];

  it("renders rows from Person[] data", () => {
    render(<ContactTable data={people} />);
    const rows = screen.getAllByTestId("contact-row");
    expect(rows).toHaveLength(2);
  });

  it("sorts by column header click", () => {
    render(<ContactTable data={people} />);
    const nameHeader = screen.getByTestId("header-name");
    fireEvent.click(nameHeader);
    // Should sort — verify no crash
    expect(screen.getAllByTestId("contact-row")).toHaveLength(2);
  });

  it("filters by search input", () => {
    render(<ContactTable data={people} />);
    const search = screen.getByTestId("contact-search");
    fireEvent.change(search, { target: { value: "Jane" } });
    expect(screen.getAllByTestId("contact-row")).toHaveLength(1);
  });
});

describe("ConversationList", () => {
  const convos: Conversation[] = [
    {
      id: 1,
      status: "open",
      inbox_id: 1,
      contact: { id: 1, name: "Alice" },
      messages_count: 5,
      unread_count: 2,
      last_activity_at: "2026-01-01T00:00:00Z",
    },
    {
      id: 2,
      status: "resolved",
      inbox_id: 1,
      contact: { id: 2, name: "Bob" },
      messages_count: 3,
      unread_count: 0,
      last_activity_at: "2026-01-01T00:00:00Z",
    },
  ];

  it("renders conversations with status badges", () => {
    render(<ConversationList conversations={convos} />);
    const badges = screen.getAllByTestId("status-badge");
    expect(badges).toHaveLength(2);
    expect(badges[0]).toHaveTextContent("open");
  });

  it("shows unread count", () => {
    render(<ConversationList conversations={convos} />);
    expect(screen.getByTestId("unread-badge")).toHaveTextContent("2 unread");
  });

  it("clicking opens conversation", () => {
    const onSelect = vi.fn();
    render(<ConversationList conversations={convos} onSelect={onSelect} />);
    fireEvent.click(screen.getAllByTestId("conversation-item")[0]);
    expect(onSelect).toHaveBeenCalledWith(convos[0]);
  });
});

describe("ConversationThread", () => {
  const messages: Message[] = [
    {
      id: 1,
      content: "Hello",
      message_type: "incoming",
      sender: { id: 1, name: "Alice", type: "contact" },
      created_at: "2026-01-01T00:00:00Z",
      private: false,
    },
    {
      id: 2,
      content: "Hi there",
      message_type: "outgoing",
      sender: { id: 2, name: "Robothor", type: "agent" },
      created_at: "2026-01-01T00:01:00Z",
      private: false,
    },
  ];

  it("renders message bubbles", () => {
    render(<ConversationThread messages={messages} />);
    expect(screen.getAllByTestId("message-bubble")).toHaveLength(2);
  });

  it("renders title when provided", () => {
    render(<ConversationThread messages={messages} title="Chat with Alice" />);
    expect(screen.getByText("Chat with Alice")).toBeInTheDocument();
  });
});

describe("MetricCard", () => {
  it("renders title and value", () => {
    render(<MetricCard title="Total Contacts" value={42} />);
    expect(screen.getByText("Total Contacts")).toBeInTheDocument();
    expect(screen.getByTestId("metric-value")).toHaveTextContent("42");
  });
});

describe("MetricGrid", () => {
  it("renders grid of metrics", () => {
    const metrics = [
      { title: "A", value: 1 },
      { title: "B", value: 2 },
    ];
    render(<MetricGrid metrics={metrics} />);
    const cards = screen.getAllByTestId("metric-card");
    expect(cards).toHaveLength(2);
  });
});

describe("BarChart", () => {
  it("renders chart container", () => {
    render(<BarChart title="Revenue" data={[]} dataKey="value" />);
    expect(screen.getByTestId("bar-chart")).toBeInTheDocument();
  });
});

describe("LineChart", () => {
  it("renders chart container", () => {
    render(<LineChart title="Trend" data={[]} dataKey="value" />);
    expect(screen.getByTestId("line-chart")).toBeInTheDocument();
  });
});

describe("PieChart", () => {
  it("renders chart container", () => {
    render(<PieChart title="Distribution" data={[]} />);
    expect(screen.getByTestId("pie-chart")).toBeInTheDocument();
  });
});

describe("Timeline", () => {
  it("renders events", () => {
    const events = [
      { id: "1", title: "Event 1", timestamp: "2026-01-01T00:00:00Z" },
      { id: "2", title: "Event 2", timestamp: "2026-01-02T00:00:00Z" },
    ];
    render(<Timeline events={events} />);
    expect(screen.getAllByTestId("timeline-event")).toHaveLength(2);
  });
});

describe("MemorySearch", () => {
  it("renders search results", () => {
    const results: MemorySearchResult[] = [
      {
        content: "CRM was deployed",
        category: "technical",
        created_at: "2026-01-01T00:00:00Z",
        similarity: 0.85,
      },
    ];
    render(<MemorySearch results={results} query="CRM" />);
    expect(screen.getByTestId("memory-result")).toBeInTheDocument();
    expect(screen.getByText("CRM was deployed")).toBeInTheDocument();
    expect(screen.getByText("85% match")).toBeInTheDocument();
  });

  it("shows no results message", () => {
    render(<MemorySearch results={[]} query="nothing" />);
    expect(screen.getByText("No results found.")).toBeInTheDocument();
  });
});

describe("ServiceHealth", () => {
  it("renders service cards", () => {
    const services = [
      { name: "bridge", url: "http://localhost:9100/health", status: "healthy" as const, responseTime: 5 },
      { name: "vision", url: "http://localhost:8600/health", status: "unhealthy" as const },
    ];
    render(<ServiceHealth services={services} overallStatus="degraded" />);
    expect(screen.getAllByTestId("service-card")).toHaveLength(2);
    expect(screen.getByTestId("overall-status")).toHaveTextContent("Degraded");
  });
});

describe("TaskBoard", () => {
  it("renders tasks in columns", () => {
    const tasks = [
      { id: "1", title: "Task 1", status: "TODO" as const },
      { id: "2", title: "Task 2", status: "IN_PROGRESS" as const },
      { id: "3", title: "Task 3", status: "DONE" as const },
    ];
    render(<TaskBoard tasks={tasks} />);
    expect(screen.getAllByTestId("task-card")).toHaveLength(3);
  });
});

describe("MarkdownView", () => {
  it("renders markdown content", () => {
    render(<MarkdownView content="**Bold text**" title="Notes" />);
    expect(screen.getByTestId("markdown-view")).toBeInTheDocument();
    expect(screen.getByText("Notes")).toBeInTheDocument();
  });
});

describe("FormPanel", () => {
  it("renders form fields", () => {
    const fields = [
      { name: "name", label: "Name", required: true },
      { name: "email", label: "Email" },
    ];
    render(<FormPanel title="New Contact" fields={fields} onSubmit={vi.fn()} />);
    expect(screen.getByTestId("field-name")).toBeInTheDocument();
    expect(screen.getByTestId("field-email")).toBeInTheDocument();
  });

  it("calls onSubmit with form data", () => {
    const onSubmit = vi.fn();
    const fields = [{ name: "name", label: "Name" }];
    render(<FormPanel title="Test" fields={fields} onSubmit={onSubmit} />);

    fireEvent.change(screen.getByTestId("field-name"), {
      target: { value: "John" },
    });
    fireEvent.click(screen.getByTestId("form-submit"));
    expect(onSubmit).toHaveBeenCalledWith({ name: "John" });
  });
});
