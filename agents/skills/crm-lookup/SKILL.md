---
name: crm-lookup
description: Look up a contact, company, or conversation in the CRM.
---

# CRM Lookup

Search and display CRM records -- people, companies, conversations, notes.

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

## Rules

- Never expose internal UUIDs to the user unless debugging
- If multiple matches, list them and ask for clarification
- If no matches, suggest creating a new record
