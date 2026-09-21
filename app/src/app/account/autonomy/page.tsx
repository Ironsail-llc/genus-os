import Link from "next/link";
import { notFound } from "next/navigation";
import { PersonalAutomationPanel } from "@/components/personal-automation-panel";
import { personalAutomationEnabled } from "@/lib/autonomy";

/**
 * `/account/autonomy` — enrol private information and delegate authority.
 *
 * 404 unless this instance offers personal automation
 * (`ROBOTHOR_AUTONOMY_ENABLED`). The route shipped unconditionally: every
 * instance served a page offering to store payment cards and grant an agent
 * spending authority, whether or not the operator had ever heard of the
 * feature. The same answer the `/setup` wizard gives once an owner exists —
 * a route for a capability this appliance does not offer is not a disabled
 * button, it is not there.
 *
 * This is a UX gate; the bridge authorizes every `/api/autonomy/*` call
 * independently.
 */

export const dynamic = "force-dynamic";

export default function PersonalAutomationPage() {
  if (!personalAutomationEnabled()) notFound();

  return <main className="min-h-screen bg-background p-6">
    <div className="mx-auto max-w-2xl space-y-6 py-8">
      <h1 className="text-2xl font-semibold">Personal automation</h1>
      <p>Give your assistant the information and authority it needs to finish tasks for you.</p>
      <PersonalAutomationPanel />
      <Link className="underline" href="/">Back to Genus OS</Link>
    </div>
  </main>;
}
