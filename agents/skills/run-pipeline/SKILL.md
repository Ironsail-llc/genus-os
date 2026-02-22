---
name: run-pipeline
description: Trigger intelligence pipeline tiers (ingestion, analysis, or deep analysis).
---

# Run Pipeline

Manually trigger intelligence pipeline tiers.

## Inputs

- **tier**: 1, 2, or 3 (required)
  - Tier 1: Continuous ingestion (incremental, fast)
  - Tier 2: Periodic analysis (meeting prep, memory blocks, entities)
  - Tier 3: Deep analysis (relationships, patterns, quality audit)

## Execution

### Tier 1 -- Continuous Ingestion
```sh
robothor pipeline --tier 1
```

### Tier 2 -- Periodic Analysis
```sh
robothor pipeline --tier 2
```

### Tier 3 -- Deep Analysis
```sh
robothor pipeline --tier 3
```

## Output

Report:
- Which tier was run
- Duration
- Key metrics (items processed, entities discovered, facts stored)
- Any errors encountered

## Rules

- Tier 3 is slow (can take 10+ minutes) -- warn before running
- If a tier is already running (check process list), do not start another
- Always report the output, even if empty
