import { createRoot } from "react-dom/client";
import { GoalsView } from "../../src/components/views/goals-view";
import "../../src/app/globals.css";

function Acceptance() {
  return <main className="min-h-screen bg-background text-foreground p-6">
    <aside className="border rounded p-4 mb-6 space-y-2">
      <h1 className="text-xl font-semibold">Local acceptance workspace</h1>
      <p>Synthetic examples using the real goal controls and an isolated database. Models and business connectors are off.</p>
      <p>Try pausing the parent and checking its child, revising criteria, adding a milestone, and approving the draft.</p>
      <button className="border rounded px-3 py-1" onClick={async () => {
        const response = await fetch("/uat/reset", { method: "POST" });
        if (response.ok) window.location.reload();
      }}>Reset synthetic examples</button>
    </aside>
    <GoalsView visible />
  </main>;
}

createRoot(document.getElementById("root")!).render(<Acceptance />);
