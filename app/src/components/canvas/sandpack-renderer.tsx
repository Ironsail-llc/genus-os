"use client";

import {
  SandpackProvider,
  SandpackPreview,
} from "@codesandbox/sandpack-react";
import { useMemo } from "react";

interface SandpackRendererProps {
  code: string;
}

export function SandpackRenderer({ code }: SandpackRendererProps) {
  const files = useMemo(
    () => ({
      "/App.tsx": code,
      // Stub for business component imports — in Sandpack we render with basic HTML
      "/components/business.tsx": BUSINESS_STUB,
    }),
    [code]
  );

  return (
    <div className="w-full h-full" data-testid="sandpack-renderer">
      <SandpackProvider
        template="react-ts"
        files={files}
        theme="dark"
        options={{
          externalResources: [
            "https://cdn.tailwindcss.com",
          ],
        }}
        customSetup={{
          dependencies: {
            recharts: "^2.12.0",
            "lucide-react": "^0.400.0",
          },
        }}
      >
        <SandpackPreview
          showNavigator={false}
          showOpenInCodeSandbox={false}
          style={{ height: "100%" }}
        />
      </SandpackProvider>
    </div>
  );
}

/** Minimal stubs so generated code that imports from business components doesn't crash */
const BUSINESS_STUB = `
import React from "react";

// Stub implementations of business components for Sandpack
export function MetricCard({ label, value, description, trend, trendValue }: any) {
  const trendColor = trend === "up" ? "text-green-400" : trend === "down" ? "text-red-400" : "text-zinc-400";
  return (
    <div className="p-4 rounded-lg border border-zinc-800 bg-zinc-900/50">
      <p className="text-xs text-zinc-400">{label}</p>
      <p className="text-2xl font-bold text-zinc-100">{value}</p>
      {description && <p className="text-xs text-zinc-500 mt-1">{description}</p>}
      {trendValue && <p className={\`text-xs mt-1 \${trendColor}\`}>{trendValue}</p>}
    </div>
  );
}

export function MetricGrid({ metrics }: any) {
  return (
    <div className="grid grid-cols-2 md:grid-cols-3 gap-4">
      {metrics?.map((m: any, i: number) => <MetricCard key={i} {...m} />)}
    </div>
  );
}

export function ServiceHealth({ services, overallStatus }: any) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <div className={\`w-2 h-2 rounded-full \${overallStatus === "ok" ? "bg-green-500" : "bg-yellow-500"}\`} />
        <span className="text-sm text-zinc-300">System {overallStatus}</span>
      </div>
      <div className="grid grid-cols-3 gap-2">
        {services?.map((s: any) => (
          <div key={s.name} className="flex items-center gap-2 text-xs">
            <div className={\`w-1.5 h-1.5 rounded-full \${s.status === "healthy" ? "bg-green-500" : "bg-red-500"}\`} />
            <span className="text-zinc-400">{s.name}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function ContactTable({ data }: any) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-zinc-800">
            <th className="text-left p-2 text-zinc-400">Name</th>
            <th className="text-left p-2 text-zinc-400">Title</th>
            <th className="text-left p-2 text-zinc-400">Company</th>
            <th className="text-left p-2 text-zinc-400">Email</th>
          </tr>
        </thead>
        <tbody>
          {data?.slice(0, 20).map((p: any) => (
            <tr key={p.id} className="border-b border-zinc-800/50">
              <td className="p-2 text-zinc-100">{p.name?.firstName} {p.name?.lastName}</td>
              <td className="p-2 text-zinc-400">{p.jobTitle || "—"}</td>
              <td className="p-2 text-zinc-400">{p.company?.name || "—"}</td>
              <td className="p-2 text-zinc-400">{p.email || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ConversationList({ conversations }: any) {
  return (
    <div className="space-y-2">
      {conversations?.map((c: any) => (
        <div key={c.id} className="p-3 rounded-lg border border-zinc-800 bg-zinc-900/50">
          <div className="flex justify-between">
            <span className="text-sm text-zinc-100">{c.contact?.name || "Unknown"}</span>
            <span className="text-xs text-zinc-500">{c.status}</span>
          </div>
          <p className="text-xs text-zinc-400 mt-1">{c.messages_count} messages, {c.unread_count} unread</p>
        </div>
      ))}
    </div>
  );
}

export function MarkdownView({ content, title }: any) {
  return (
    <div className="prose prose-invert prose-sm max-w-none">
      {title && <h2>{title}</h2>}
      <pre className="whitespace-pre-wrap text-zinc-300">{content}</pre>
    </div>
  );
}

export function Timeline({ events }: any) {
  return (
    <div className="space-y-3 border-l-2 border-zinc-800 pl-4">
      {events?.map((e: any, i: number) => (
        <div key={i}>
          <p className="text-sm text-zinc-100">{e.title}</p>
          {e.description && <p className="text-xs text-zinc-400">{e.description}</p>}
          <p className="text-xs text-zinc-500">{e.timestamp}</p>
        </div>
      ))}
    </div>
  );
}

// Re-export everything else as simple passthrough
export const ContactCard = ({ name, email, jobTitle }: any) => (
  <div className="p-4 rounded-lg border border-zinc-800">
    <p className="text-zinc-100 font-medium">{name?.firstName} {name?.lastName}</p>
    {jobTitle && <p className="text-xs text-zinc-400">{jobTitle}</p>}
    {email && <p className="text-xs text-zinc-500">{email}</p>}
  </div>
);
export const CompanyCard = ({ name, domainName }: any) => (
  <div className="p-4 rounded-lg border border-zinc-800">
    <p className="text-zinc-100 font-medium">{name}</p>
    {domainName && <p className="text-xs text-zinc-400">{domainName}</p>}
  </div>
);
export const ConversationThread = ({ messages }: any) => (
  <div className="space-y-2">{messages?.map((m: any) => <div key={m.id} className="text-sm text-zinc-300">{m.content}</div>)}</div>
);
export const BarChart = () => <div className="text-zinc-500 p-4">Chart unavailable in preview</div>;
export const LineChart = () => <div className="text-zinc-500 p-4">Chart unavailable in preview</div>;
export const PieChart = () => <div className="text-zinc-500 p-4">Chart unavailable in preview</div>;
export const DataTable = ({ columns, data }: any) => (
  <table className="w-full text-sm"><thead><tr>{columns?.map((c: any) => <th key={c.key} className="text-left p-2 text-zinc-400">{c.label}</th>)}</tr></thead>
  <tbody>{data?.map((r: any, i: number) => <tr key={i}>{columns?.map((c: any) => <td key={c.key} className="p-2 text-zinc-300">{String(r[c.key] ?? "")}</td>)}</tr>)}</tbody></table>
);
export const MemorySearch = ({ results, query }: any) => (
  <div><p className="text-sm text-zinc-400 mb-2">Results for: {query}</p>{results?.map((r: any, i: number) => <div key={i} className="p-2 border-b border-zinc-800 text-sm text-zinc-300">{r.content}</div>)}</div>
);
export const TaskBoard = ({ tasks }: any) => (
  <div className="grid grid-cols-3 gap-4">{["todo","in_progress","done"].map(s => <div key={s}><h3 className="text-xs text-zinc-400 mb-2">{s}</h3>{tasks?.filter((t: any) => t.status === s).map((t: any) => <div key={t.id} className="p-2 rounded border border-zinc-800 text-sm mb-1">{t.title}</div>)}</div>)}</div>
);
export const FormPanel = ({ title }: any) => <div className="p-4 text-zinc-400">Form: {title}</div>;
`;
