---
name: crm-lookup
description: "Look up a contact, company, or conversation in the CRM \u2014 with exhaustive\
  \ discovery fallbacks when the target isn't in our records."
---

# CRM Lookup

Search and display CRM records — people, companies, conversations, notes. When the target isn't found, follow the full discovery chain before escalating.

---

## ⚠️ Pre-Action Checklist — Read BEFORE Any Tool Calls

**Every lookup session, run through these BEFORE your first search. No exceptions.**

1. **Disambiguate common words.** Is the company/contact name also a common English word (e.g., "Quartz", "Apple", "Delta", "Oracle")? If yes, ALWAYS include industry terms in web searches: `"Quartz Diagnostics healthcare"` not `"Quartz"`.

2. **Never re-search the same channel.** If Gmail/CRM/Apollo returned nothing on the first query, move to the NEXT channel in the fallback chain. Retrying the same data source with slightly different terms is the #1 time-waster in lookups.

3. **Check if you already have the data.** Before searching, check: is this person/company already in your current session context, a recent task body, or a memory block you just read? Don't re-search for data you already hold.

4. **Know your ID types.** CRM task UUIDs (`4d2b9d3e-...`) ≠ conversation IDs (integers). Passing a UUID to `list_messages` or `get_conversation` wastes a turn with a silent `InvalidTextRepresentation` error.

---

## Inputs

- **query**: Name, email, company, or keyword to search for (required)

## Execution

1. Search across CRM:
```
search_records(query="<QUERY>")
```

2. For person results, get full details:
```
get_person(id="<UUID>")
```

3. For company results:
```
get_company(id="<UUID>")
```

4. Check for related conversations:
```
list_conversations(status="open")
```

5. Check for related notes:
```
list_notes(personId="<UUID>")
```

## Output Format

Present results as a concise summary:
- Name, email, phone, company, job title
- Recent conversations (last 3)
- Recent notes (last 3)
- Any linked entities from the knowledge graph

## Contact Discovery — Full Fallback Chain

When the contact isn't in our CRM and you need to reach them, follow this chain **in order**. Do NOT skip steps, and do NOT repeat a step that already returned empty.

1. **CRM / `search_records`** — check crm_people and crm_companies
2. **Gmail history** — `gws_gmail_search` for past threads with this sender/company
3. **Memory** — `search_memory` for prior mentions, stored email addresses, entity graph
4. **Apollo** — `apollo_search_people` / `apollo_search_companies` (free search, no credits)
5. **Company website via browser** — navigate to the company's website, look for:
   - Contact page, "About Us", or footer with email/form
   - Specific person's profile/team page for direct email
   - Use `browser` tool (start → navigate → snapshot) for JS-heavy sites
6. **Web search for the specific contact** — ALWAYS include industry context for disambiguation (see checklist #1)
7. **Apollo enrich** — `apollo_enrich_person` (costs credits — only after steps 1-6 are exhausted)

Only escalate to the operator after exhausting this entire chain.

## Pitfalls

- **Don't search for minerals.** If the company name is also a common word (e.g., "Quartz"), include industry terms in web searches to disambiguate (e.g., "Quartz Diagnostics healthcare" not just "Quartz").
- **Don't re-search the same channel.** If Gmail/CRM/Apollo returned nothing on the first query, don't retry with slightly different terms — move to the next channel. Retrying the same data source is the most common time-waster.
- **Draft the message content before escalating.** If you truly can't find contact info, prepare the exact reply text and present it alongside the ask. The operator's job should be "forward this email" or "add this recipient" — not "compose the whole thing." Minimal operator effort is the goal.
- **Tasks ≠ Conversations ≠ Messages — don't cross the streams.** CRM task UUIDs (e.g. `4d2b9d3e-...`) are **not** valid inputs to `list_messages`, `get_conversation`, or `create_message` — those expect integer conversation IDs. Similarly, conversation IDs are integers, not UUIDs. If you need to find a conversation related to a task, use `search_records` with a keyword from the task title/description, or `list_conversations` and browse. Passing a UUID to an integer field produces `InvalidTextRepresentation` — a silent, non-blocking error that wastes a turn.
- **Apollo 403 ≠ bad credentials.** When Apollo returns a 403, do NOT assume the API key is invalid. Apollo has deprecated endpoints in the past (e.g. `/v1/mixed_people/search` returning 403 while `/api/v1/mixed_people/api_search` works fine with the same key). **Diagnosis sequence:** (1) test the key against a known-good endpoint with `curl` — `curl -s -o /dev/null -w '%{http_code}' -H "Content-Type: application/json" -d '{"q_person_name":"test","per_page":1}' "https://api.apollo.io/api/v1/mixed_people/api_search?api_key=$APOLLO_API_KEY"` — if 200, the key is valid and the tool's routing is wrong. (2) Check the tool source for which endpoint it calls. (3) File a bug or patch the endpoint path. This pattern applies to any external API tool: a 403 can mean "endpoint moved" not "you're unauthorized."

## Rules

- Never expose internal UUIDs to the user unless debugging
- If multiple matches, list them and ask for clarification
- If no matches after the full discovery chain, present what you've drafted and ask for the specific missing piece (email address, forwarding, etc.)
