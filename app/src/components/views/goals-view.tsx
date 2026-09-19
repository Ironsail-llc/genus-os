"use client";

import { useCallback, useEffect, useState } from "react";

type GoalStatus = "queued" | "running" | "waiting" | "blocked" | "paused" | "review" | "complete" | "canceled";
interface Evidence { criterion: number; summary: string; reference: string; satisfied: boolean }
interface Goal {
  id: string; objective: string; kind: "short" | "long"; mode: "finite" | "ongoing";
  status: GoalStatus; version: number; success_criteria: string[]; parent_goal_id: string | null;
  checkpoint: string; next_action: string; blocker: string; ready_at: string;
  tokens_used: number; token_budget: number | null; cost_usd: number;
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

export function GoalsView({ visible }: { visible: boolean }) {
  const [goals, setGoals] = useState<Goal[]>([]);
  const [selected, setSelected] = useState<Goal | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [objective, setObjective] = useState("");
  const [criteria, setCriteria] = useState("");
  const [kind, setKind] = useState<"short" | "long">("short");
  const [ongoing, setOngoing] = useState(false);
  const [humanReview, setHumanReview] = useState(false);
  const [budget, setBudget] = useState("");
  const [resumeBudget, setResumeBudget] = useState("");
  const [cadence, setCadence] = useState("24");
  const [steer, setSteer] = useState("");

  const refresh = useCallback(async () => {
    const data = await api();
    setGoals(data.goals); setEnabled(data.enabled); setLoaded(true);
  }, []);

  useEffect(() => {
    if (!visible) return;
    let disposed = false;
    const poll = async () => {
      try {
        const data = await api();
        if (!disposed) { setGoals(data.goals); setEnabled(data.enabled); setLoaded(true); }
      } catch (e) { if (!disposed) setError((e as Error).message); }
    };
    void poll();
    const timer = setInterval(() => void poll(), 5000);
    return () => { disposed = true; clearInterval(timer); };
  }, [visible]);

  async function perform(fn: () => Promise<void>) {
    setBusy(true); setError("");
    try { await fn(); await refresh(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }

  async function select(goalId: string) {
    await perform(async () => setSelected((await api(`/${goalId}`)).goal));
  }

  async function change(action: string) {
    if (!selected) return;
    await perform(async () => {
      const data = await api(`/${selected.id}`, { method: "PATCH", body: JSON.stringify({
        action, version: selected.version, ...(action === "steer" ? { note: steer } : {}),
        ...(action === "resume" && resumeBudget ? { token_budget: Number(resumeBudget) } : {}),
      }) });
      setSelected((await api(`/${data.goal.id}`)).goal); setSteer("");
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
          }) });
          setSelected(data.goal); setObjective(""); setCriteria("");
        });
      }}>
        <label className="block">Objective<input required value={objective} onChange={e => setObjective(e.target.value)} className="block w-full border rounded p-2 bg-background" /></label>
        <label className="block">Success criteria, one per line<textarea required value={criteria} onChange={e => setCriteria(e.target.value)} className="block w-full border rounded p-2 bg-background" /></label>
        <div className="flex flex-wrap gap-4 items-center">
          <label>Goal type <select value={kind} onChange={e => setKind(e.target.value as "short" | "long")} className="border rounded p-2 bg-background"><option value="short">Short-term</option><option value="long">Long-term</option></select></label>
          {kind === "long" && <><label><input type="checkbox" checked={ongoing} onChange={e => setOngoing(e.target.checked)} /> Ongoing target</label><label>Review every (hours) <input type="number" min="1" required value={cadence} onChange={e => setCadence(e.target.value)} className="w-20 border rounded p-2 bg-background" /></label></>}
          <label>Optional token budget <input type="number" min="1" value={budget} onChange={e => setBudget(e.target.value)} className="w-28 border rounded p-2 bg-background" /></label>
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
          <p>{selected.status} · {selected.tokens_used.toLocaleString()} tokens{selected.token_budget ? ` / ${selected.token_budget.toLocaleString()}` : ""} · ${selected.cost_usd.toFixed(4)}</p>
          {selected.parent_goal_id && <button className="underline" onClick={() => void select(selected.parent_goal_id!)}>Open parent goal</button>}
          <p>{selected.checkpoint}</p><p>{selected.next_action}</p>
          {selected.wait && <p>Waiting: {selected.wait.reason}. Next review: {new Date(selected.ready_at).toLocaleString()}</p>}
          {selected.assessment && <p>Assessment: {selected.assessment.status} — {selected.assessment.note}</p>}
          <h3 className="font-medium">Criteria and evidence</h3>
          <ol className="list-decimal pl-5 space-y-2">{selected.success_criteria.map((criterion, i) => <li key={i}>{criterion}
            {selected.evidence.filter(e => e.criterion === i).map((e, j) => <p key={j} className="text-sm">{e.satisfied ? "Verified" : "Unmet"}: {e.summary} ({e.reference})</p>)}
          </li>)}</ol>
          {selected.status === "blocked" && <label>Updated token budget (optional)<input type="number" min="1" value={resumeBudget} onChange={e => setResumeBudget(e.target.value)} className="block border rounded p-2 bg-background" /></label>}
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
