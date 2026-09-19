import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { PersonalAutomationPanel } from "../personal-automation-panel";

let posted: Record<string, unknown> | null;
beforeEach(() => {
  posted=null;
  window.history.replaceState({},"","/account/autonomy");
  vi.stubGlobal("fetch",vi.fn(async (url:string,init?:RequestInit) => {
    if(url.endsWith("/grants") && init?.method === "POST") posted=JSON.parse(String(init.body));
    return {ok:true,json:async () => url.endsWith("/status") ? {resources:[],grants:[],settings:{enabled:true,managed_browser:false,payment_processing:false}} : {operations:[]}};
  }));
});
afterEach(()=>{cleanup();vi.unstubAllGlobals();});

it("stores the operator's allowed purposes with the standing grant",async()=>{
  render(<PersonalAutomationPanel/>);
  await waitFor(()=>expect((screen.getByText("Grant authority") as HTMLButtonElement).disabled).toBe(false));
  fireEvent.change(screen.getByLabelText("Allowed purposes, one per line"),{target:{value:"Personal memberships\nTravel bookings"}});
  fireEvent.change(screen.getByLabelText("Websites, one per line"),{target:{value:"https://club.example"}});
  fireEvent.change(screen.getByLabelText("Authority expires"),{target:{value:"2030-01-01"}});
  fireEvent.click(screen.getByText("Grant authority"));
  await waitFor(()=>expect(posted?.allowed_purposes).toEqual(["Personal memberships","Travel bookings"]));
});
