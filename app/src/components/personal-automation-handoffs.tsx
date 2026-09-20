"use client";

import { useState } from "react";

export type ExternalHandoff = {
  id: string; operation_id: string; kind: string; state: string;
  origin: string; purpose: string; expires_at: string;
};
const kinds: Record<string,string> = {
  sms:"SMS verification", push:"Device approval", passkey:"Passkey verification",
  biometric:"Biometric verification", issuer:"Issuer verification", captcha:"Website challenge",
};
export function PersonalAutomationHandoffs({handoffs,refresh}: {handoffs:ExternalHandoff[];refresh:()=>Promise<void>|void}) {
  const [busy,setBusy]=useState<string|null>(null);
  const [message,setMessage]=useState("");
  const pending=handoffs.filter(item=>item.state!=="resolved");
  async function check(id:string) {
    setBusy(id);setMessage("");
    try {
      const response=await fetch(`/api/bridge/api/autonomy/handoffs/${id}/check`,{method:"POST",cache:"no-store"});
      if(!response.ok) throw new Error("Status check unavailable. Refresh the page to check whether this handoff expired.");
      await refresh();
      setMessage("Status check requested. Completion still needs website confirmation.");
    } catch(error) {setMessage(error instanceof Error?error.message:"Status check unavailable.");}
    finally {setBusy(null);}
  }
  if(!pending.length) return null;
  return <section className="space-y-3">
    <h2 className="text-lg font-medium">External verification</h2>
    <p className="text-sm">Complete the pending verification using the merchant’s or issuer’s trusted website or device. Keep verification codes out of chat.</p>
    {message&&<p role="status">{message}</p>}
    {pending.map(item=><div key={item.id} className="space-y-2 rounded border p-3">
      <p className="font-medium">{kinds[item.kind]||"External verification"}</p>
      <p>{item.purpose}</p><p className="text-sm text-muted-foreground">{item.origin}</p>
      {item.state==="expired"?<p>This verification handoff expired. The task is waiting for you to clear it.</p>:<>
        {item.state==="unconfirmed"&&<p role="status" className="text-sm font-medium">
          We checked this three times and still could not tell whether it completed. Confirm on
          the merchant’s or issuer’s own website before checking again.
        </p>}
        <p className="text-sm">Status checks available until {new Date(item.expires_at).toLocaleString()}.</p>
        <button type="button" disabled={busy!==null} className="rounded border px-3 py-2" onClick={()=>void check(item.id)}>
          {busy===item.id?"Requesting status check…":item.state==="unconfirmed"?"Check again":"Check status after verification"}
        </button>
      </>}
    </div>)}
  </section>;
}
