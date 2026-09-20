// Public SDK transport contract only. No server, provider or business tool is invoked.
import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { createOpencodeClient } from "@opencode-ai/sdk";

const requests = [];
const client = createOpencodeClient({
  baseUrl: "http://synthetic.invalid",
  throwOnError: true,
  fetch: async (request) => {
    const body = request.method === "GET" ? null : await request.json().catch(() => null);
    requests.push({ method: request.method, path: new URL(request.url).pathname, body });
    return new Response(JSON.stringify({ id: "fixture-session" }), { headers: { "Content-Type": "application/json" } });
  },
});
await client.session.create({ body: { title: "Synthetic runtime feasibility" } });
await client.session.prompt({ path: { id: "fixture-session" }, body: {
  model: { providerID: "fixture", modelID: "fixture" },
  tools: { bash: false, edit: false, write: false, task: false, record: true },
  parts: [{ type: "text", text: "Use only the authorized record gateway." }],
} });
await client.session.abort({ path: { id: "fixture-session" } });
assert.deepEqual(requests.map(r => r.path), ["/session", "/session/fixture-session/message", "/session/fixture-session/abort"]);
assert.equal(requests[1].body.tools.bash, false);
assert.equal(requests[1].body.tools.record, true);
writeFileSync(new URL("result.json", import.meta.url), JSON.stringify({
  scope: "SDK serialization against synthetic transport; no server-side enforcement claim",
  transport_contract: "passed",
  promotion_eligible: false,
  unverified: ["all built-in tools denied at dispatch", "trusted tenant and principal binding", "pre-provider shared budget reservation", "durable stop and compatible recovery"],
  requests,
}, null, 2) + "\n");
console.log("OpenCode SDK transport contract passed; runtime enforcement is unverified.");
