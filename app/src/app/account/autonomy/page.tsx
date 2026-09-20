import Link from "next/link";
import { PersonalAutomationPanel } from "@/components/personal-automation-panel";

export const dynamic = "force-dynamic";

export default function PersonalAutomationPage() {
  return <main className="min-h-screen bg-background p-6">
    <div className="mx-auto max-w-2xl space-y-6 py-8">
      <h1 className="text-2xl font-semibold">Personal automation</h1>
      <p>Give your assistant the information and authority it needs to finish tasks for you.</p>
      <PersonalAutomationPanel />
      <Link className="underline" href="/">Back to Genus OS</Link>
    </div>
  </main>;
}
