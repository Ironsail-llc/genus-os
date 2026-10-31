"use client";

import { useCallback, useState } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useAppQueryClient } from "@/lib/query/query-client";

type GoalStatus = "queued" | "running" | "waiting" | "blocked" | "paused" | "review" | "complete" | "canceled";
interface Evidence { criterion: number; summary: string; reference: string; satisfied: boolean }
interface Goal {
  id: string; objective: string; kind: "short" | "long"; mode: "finite" | "ongoing";
  status: GoalStatus; version: number; success_criteria: string[]; parent_goal_id: string | null;
  checkpoint: string; next_action: string; blocker: string; ready_at: string;
  tokens_used: number; token_budget: number | null; cost_usd: number;
  cost_budget_usd: number | null; attempts: number; max_attempts: number | null; deadline_at: string | null;
  evidence: Evidence[]; wait: { reason: string; event_type?: string; task_id?: string } | null;
  assessment: { status: string; note: string; at: string } | null;
  children?: Goal[];
  tasks?: { id: string; title: string; status: string }[];
  runs?: { id: string; run_id: string | null; status: string; tokens: number }[];
  history?: { action: string; actor: string; detail: Record<string, unknown>; created_at: string }[];
}

async function api(path = "", init?: RequestInit) {
  const response = await fetch(`/api/bridge/api/goals${path}`, {
    ...init, headers: { "Content-Type": "application/json", ...init?.headers },
  });
  const body = await response.json();
  if (!response.ok) {
    const detail = body.detail ?? body.error ?? `Request failed (${response.status})`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

/**
 * The list and the open goal are re-read every 30 seconds while the view is
 * on screen and the tab is visible. There is no goal event on the stream yet;
 * when there is, it should invalidate these keys and the beat can slow down.
 */
const POLL_MS = 30_000;
const GOALS_KEY = ["bridge", "goals"] as const;
type GoalList = { goals: Goal[]; enabled: boolean };
type Snapshot = { list: GoalList; detail: Goal | null };
async function readSnapshot(goalId: string | null, signal?: AbortSignal): Promise<Snapshot> {
  const list: GoalList = await api("", { signal });
  const detail = goalId ? ((await api(`/${goalId}`, { signal })).goal as Goal) : null;
  return { list, detail };
}

export function GoalsView({ visible }: { visible: boolean }) {
  const client = useAppQueryClient();
  const [chosen, setSelected] = useState<Goal | null>(null);
  const [actionError, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [objective, setObjective] = useState("");
  const [criteria, setCriteria] = useState("");
  const [kind, setKind] = useState<"short" | "long">("short");
  const [ongoing, setOngoing] = useState(false);
  const [humanReview, setHumanReview] = useState(false);
  const [budget, setBudget] = useState("");
  const [costBudget, setCostBudget] = useState("");
  const [maxAttempts, setMaxAttempts] = useState("");
  const [deadlineDays, setDeadlineDays] = useState("");
  const [resumeBudget, setResumeBudget] = useState("");
  const [resumeCost, setResumeCost] = useState("");
  const [resumeAttempts, setResumeAttempts] = useState("");
  const [resumeDays, setResumeDays] = useState("");
  const [cadence, setCadence] = useState("24");
  const [steer, setSteer] = useState("");

  // The list and the open goal are one read on one beat, as they always were:
  // list first, then the open goal's detail.
  const selectedId = chosen?.id ?? null;
  const snapshotKey = [...GOALS_KEY, selectedId] as const;
  const snapshot = useQuery(
    {
      queryKey: snapshotKey,
      queryFn: ({ signal }) => readSnapshot(selectedId, signal),
      enabled: visible,
      staleTime: POLL_MS,
      refetchInterval: POLL_MS,
      // Opening another goal keeps the list on screen while its read is out.
      placeholderData: keepPreviousData,
    },
    client
  );
  const goals = snapshot.data?.list.goals ?? [];
  const enabled = snapshot.data?.list.enabled ?? false;
  const loaded = snapshot.data !== undefined;

  // A polled copy replaces the open goal only when it is the same goal and no
  // older than what an action just put there.
  const polled = snapshot.data?.detail;
  const selected =
    chosen && polled && polled.id === chosen.id && polled.version >= chosen.version ? polled : chosen;
  const error = actionError || snapshot.error?.message || "";

  // After a write every goals snapshot is stale: the one on screen is read
  // again now (cancelling a poll that left before the write), and one seeded
  // by `open` is read as soon as the view switches to it.
  const refresh = useCallback(async () => {
    await client.invalidateQueries({ queryKey: GOALS_KEY }, { cancelRefetch: true, throwOnError: true });
  }, [client]);

  /** Open a goal, seeding its snapshot so the detail paints without a second read. */
  const open = useCallback(
    (goal: Goal) => {
      const list = client.getQueryData<Snapshot>([...GOALS_KEY, selectedId])?.list;
      if (list) client.setQueryData<Snapshot>([...GOALS_KEY, goal.id], { list, detail: goal });
      setSelected(goal);
    },
    [client, selectedId]
  );

  async function perform(fn: () => Promise<void>) {
    setBusy(true); setError("");
    try { await fn(); await refresh(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  async function select(goalId: string) {
    await perform(async () => open((await api(`/${goalId}`)).goal));
  }

  async function change(action: string) {
    if (!selected) return;
    await perform(async () => {
      const data = await api(`/${selected.id}`, { method: "PATCH", body: JSON.stringify({
        action, version: selected.version, ...(action === "steer" ? { note: steer } : {}),
        // A goal blocked on a ceiling only restarts once that ceiling rises.
        ...(action === "resume" && resumeBudget ? { token_budget: Number(resumeBudget) } : {}),
        ...(action === "resume" && resumeCost ? { cost_budget_usd: Number(resumeCost) } : {}),
        ...(action === "resume" && resumeAttempts ? { max_attempts: Number(resumeAttempts) } : {}),
        ...(action === "resume" && resumeDays ? { deadline_seconds: Math.round(Number(resumeDays) * 86400) } : {}),
      }) });
      open((await api(`/${data.goal.id}`)).goal); setSteer("");
    });
  }

  return (
    <div hidden={!visible} className="h-full overflow-y-auto p-4 space-y-5" data-testid="goals-view">
      <div className="flex items-center justify-between gap-3">
        <div><h1 className="text-xl font-semibold">Goals</h1><p className="text-sm text-muted-foreground">Work to finish now, and objectives to pursue over time.</p></div>
        <button disabled={busy || !loaded} className="border rounded px-3 py-2" onClick={() => void perform(async () => {
          await api("/settings", { method: "PATCH", body: JSON.stringify({ enabled: !enabled }) });
        })}>{enabled ? "Disable goal execution" : "Enable goal execution"}</button>
      </div>
      {!enabled && loaded && <p role="status">Goal execution is disabled. Goals and history remain available.</p>}
      {error && <p role="alert" className="text-destructive">{error} <button className="underline" onClick={() => void perform(refresh)}>Refresh</button></p>}
      <form className="border rounded p-4 space-y-3" onSubmit={(e) => {
        e.preventDefault();
        void perform(async () => {
          const data = await api("", { method: "POST", body: JSON.stringify({
            objective, success_criteria: criteria.split("\n").map(s => s.trim()).filter(Boolean),
            kind, mode: kind === "long" && ongoing ? "ongoing" : "finite", human_review: humanReview,
            token_budget: budget ? Number(budget) : null, review_seconds: Math.round(Number(cadence) * 3600),
            // Left blank, each ceiling takes the platform default; none of
            // them can be set to unlimited.
            cost_budget_usd: costBudget ? Number(costBudget) : null,
            max_attempts: maxAttempts ? Number(maxAttempts) : null,
            deadline_seconds: deadlineDays ? Math.round(Number(deadlineDays) * 86400) : null,
          }) });
          open(data.goal); setObjective(""); setCriteria("");
        });
      }}>
        <label className="block">Objective<input required value={objective} onChange={e => setObjective(e.target.value)} className="block w-full border rounded p-2 bg-background" /></label>
        <label className="block">Success criteria, one per line<textarea required value={criteria} onChange={e => setCriteria(e.target.value)} className="block w-full border rounded p-2 bg-background" /></label>
        <div className="flex flex-wrap gap-4 items-center">
          <label>Goal type <select value={kind} onChange={e => setKind(e.target.value as "short" | "long")} className="border rounded p-2 bg-background"><option value="short">Short-term</option><option value="long">Long-term</option></select></label>
          {kind === "long" && <><label><input type="checkbox" checked={ongoing} onChange={e => setOngoing(e.target.checked)} /> Ongoing target</label><label>Review every (hours) <input type="number" min="1" required value={cadence} onChange={e => setCadence(e.target.value)} className="w-20 border rounded p-2 bg-background" /></label></>}
          <label>Token budget <input type="number" min="1" placeholder="default" value={budget} onChange={e => setBudget(e.target.value)} className="w-28 border rounded p-2 bg-background" /></label>
          <label>Cost ceiling (USD) <input type="number" min="0.01" step="0.01" placeholder="default" value={costBudget} onChange={e => setCostBudget(e.target.value)} className="w-28 border rounded p-2 bg-background" /></label>
          <label>Max runs <input type="number" min="1" placeholder="default" value={maxAttempts} onChange={e => setMaxAttempts(e.target.value)} className="w-24 border rounded p-2 bg-background" /></label>
          <label>Deadline (days) <input type="number" min="1" placeholder="default" value={deadlineDays} onChange={e => setDeadlineDays(e.target.value)} className="w-24 border rounded p-2 bg-background" /></label>
          <label><input type="checkbox" checked={humanReview} onChange={e => setHumanReview(e.target.checked)} /> Review completion myself</label>
          <button disabled={busy} type="submit" className="border rounded px-3 py-2">Set goal</button>
        </div>
      </form>
      <div className="grid lg:grid-cols-2 gap-4">
        <div className="space-y-2">
          {loaded && goals.length === 0 && <p>No goals yet.</p>}
          {goals.map(goal => <button key={goal.id} onClick={() => void select(goal.id)} className="block w-full border rounded p-3 text-left" disabled={busy}>
            <span className="font-medium">{goal.objective}</span>
            <span className="block text-sm text-muted-foreground">{goal.kind === "short" ? "Short-term" : "Long-term"} · {goal.mode} · {goal.status}</span>
            {goal.blocker && <span className="block">{goal.blocker}</span>}
            {goal.status === "waiting" && <span className="block text-sm">{goal.wait?.reason} · Next review {new Date(goal.ready_at).toLocaleString()}</span>}
            {goal.assessment && <span className="block text-sm">Latest assessment: {goal.assessment.status}</span>}
          </button>)}
        </div>
        {selected && <section className="border rounded p-4 space-y-3" aria-label="Goal details">
          <h2 className="font-semibold">{selected.objective}</h2>
          <p>{selected.status} · {selected.tokens_used.toLocaleString()} tokens{selected.token_budget ? ` / ${selected.token_budget.toLocaleString()}` : ""} · ${selected.cost_usd.toFixed(4)}{selected.cost_budget_usd ? ` / $${selected.cost_budget_usd.toFixed(2)}` : ""} · run {selected.attempts}{selected.max_attempts ? ` / ${selected.max_attempts}` : ""}{selected.deadline_at ? ` · deadline ${new Date(selected.deadline_at).toLocaleDateString()}` : ""}</p>
          {selected.parent_goal_id && <button className="underline" onClick={() => void select(selected.parent_goal_id!)}>Open parent goal</button>}
          <p>{selected.checkpoint}</p><p>{selected.next_action}</p>
          {selected.wait && <p>Waiting: {selected.wait.reason}. Next review: {new Date(selected.ready_at).toLocaleString()}</p>}
          {selected.assessment && <p>Assessment: {selected.assessment.status} — {selected.assessment.note}</p>}
          <h3 className="font-medium">Criteria and evidence</h3>
          <ol className="list-decimal pl-5 space-y-2">{selected.success_criteria.map((criterion, i) => <li key={i}>{criterion}
            {selected.evidence.filter(e => e.criterion === i).map((e, j) => <p key={j} className="text-sm">{e.satisfied ? "Verified" : "Unmet"}: {e.summary} ({e.reference})</p>)}
          </li>)}</ol>
          {selected.status === "blocked" && <div className="flex flex-wrap gap-3 items-end">
            <label>Raise token budget<input type="number" min="1" value={resumeBudget} onChange={e => setResumeBudget(e.target.value)} className="block w-32 border rounded p-2 bg-background" /></label>
            <label>Raise cost ceiling (USD)<input type="number" min="0.01" step="0.01" value={resumeCost} onChange={e => setResumeCost(e.target.value)} className="block w-32 border rounded p-2 bg-background" /></label>
            <label>Raise max runs<input type="number" min="1" value={resumeAttempts} onChange={e => setResumeAttempts(e.target.value)} className="block w-28 border rounded p-2 bg-background" /></label>
            <label>Extend deadline (days)<input type="number" min="1" value={resumeDays} onChange={e => setResumeDays(e.target.value)} className="block w-28 border rounded p-2 bg-background" /></label>
          </div>}
          <div className="flex flex-wrap gap-2">
            <button disabled={busy} className="border rounded px-2 py-1" onClick={() => void select(selected.id)}>Refresh details</button>
            {!["complete", "canceled"].includes(selected.status) && <>
              <button disabled={busy} className="border rounded px-2 py-1" onClick={() => void change("pause")}>Pause</button>
              {["paused", "blocked", "waiting"].includes(selected.status) && <button disabled={busy} className="border rounded px-2 py-1" onClick={() => void change("resume")}>Resume</button>}
              {selected.status === "review" && <button disabled={busy} className="border rounded px-2 py-1" onClick={() => void change("approve")}>Approve completion</button>}
              <button disabled={busy} className="border rounded px-2 py-1" onClick={() => void change("cancel")}>Cancel goal</button>
            </>}
          </div>
          {!["complete", "canceled"].includes(selected.status) && <form onSubmit={e => { e.preventDefault(); void change("steer"); }}>
            <label>Direction for the agent<textarea required value={steer} onChange={e => setSteer(e.target.value)} className="block w-full border rounded p-2 bg-background" /></label>
            <button disabled={busy} className="border rounded px-2 py-1 mt-2">Update direction</button>
          </form>}
          <h3 className="font-medium">Execution goals</h3>
          {selected.children?.map(child => <button key={child.id} className="block underline text-left" onClick={() => void select(child.id)}>{child.objective} · {child.status}</button>)}
          <h3 className="font-medium">Linked tasks</h3>
          {selected.tasks?.map(task => <p key={task.id}>{task.title} · {task.status}</p>)}
          <h3 className="font-medium">Recent runs</h3>
          {selected.runs?.map(run => <p key={run.id} className="text-sm break-all">{run.run_id ?? run.id} · {run.status} · {run.tokens} tokens</p>)}
          <h3 className="font-medium">History</h3>
          {selected.history?.map((entry, i) => <details key={i}><summary>{entry.action} · {new Date(entry.created_at).toLocaleString()}</summary><pre className="whitespace-pre-wrap text-xs">{JSON.stringify(entry.detail, null, 2)}</pre></details>)}
        </section>}
      </div>
    </div>
  );
}
