---
name: memory-search
description: Search the memory system for facts and entities.
---

# Memory Search

Search the persistent memory system for stored facts, entities, and relationships.

## Inputs

- **query**: What to search for (required)
- **limit**: Maximum results to return (optional, default: 10)
- **category**: Filter by fact category (optional)

## Execution

1. Search for relevant facts:
```
search_memory(query="<QUERY>", limit=<LIMIT>)
```

2. If the query mentions a person, project, or organization, also look up the entity:
```
get_entity(name="<ENTITY_NAME>")
```

3. Present results with relevance scores and source attribution.

## Rules

- Always show confidence scores alongside results
- If no results found, suggest alternative search terms
- Group results by category when presenting multiple matches
- Include entity relationships when they add context to the answer
