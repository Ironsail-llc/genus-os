# Memory Intelligence Pipeline

## Overview

The Memory Intelligence Pipeline processes and maintains Robothor's long-term memory using **100% local inference** on the ThinkStation PGX. It runs via system crontab, not as a Moltbot agent.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     System Crontab                          │
└─────────────────────────┬───────────────────────────────────┘
                          │
        ┌─────────────────┴─────────────────┐
        │                                   │
        ▼                                   ▼
┌───────────────────┐             ┌───────────────────┐
│  maintenance.sh   │             │ intelligence_     │
│                   │             │ pipeline.py       │
│  3:00 AM daily    │             │                   │
│  No LLM needed    │             │  3:30 AM daily    │
│                   │             │  Uses Llama 3.2   │
│  • TTL expiry     │             │                   │
│  • Archival       │             │  • Email ingest   │
│  • Stats          │             │  • Task ingest    │
└───────────────────┘             │  • Contact ingest │
                                  │  • Quality tests  │
                                  └─────────┬─────────┘
                                            │
                                            ▼
                                  ┌───────────────────┐
                                  │   llm_client.py   │
                                  │                   │
                                  │  Llama 3.2 11B    │
                                  │  via Ollama       │
                                  │                   │
                                  │  Generation only  │
                                  │  No tool-calling  │
                                  └─────────┬─────────┘
                                            │
                                            ▼
                                  ┌───────────────────┐
                                  │     pgvector      │
                                  │                   │
                                  │  Semantic search  │
                                  │  Fact storage     │
                                  └───────────────────┘
```

## System Crontab Entries

```bash
# View current crontab
crontab -l

# Memory system entries:
0 3 * * *   /home/philip/clawd/memory_system/maintenance.sh
30 3 * * *  cd /home/philip/clawd/memory_system && ./venv/bin/python intelligence_pipeline.py >> logs/intelligence.log 2>&1
```

## Model Selection (2026-02-05)

After benchmarking on GB10, we selected **Llama 3.2 Vision 11B**:

| Model | Speed | VRAM | Accuracy | Status |
|-------|-------|------|----------|--------|
| **Llama 3.2 Vision 11B** | 44 tok/s | 8 GB | ✅ Excellent | **Selected** |
| Llama 3.2 3B | 101 tok/s | 2 GB | ✅ Good | Fallback |
| Qwen3-Next 80B | 37 tok/s | 50 GB | ❌ Output corrupted | Not used |

**Why Llama 3.2?**
- Clean structured output (no "thinking" field pollution)
- Accurate entity extraction and classification
- Vision capability for future use
- Good balance of speed and quality

**Why NOT Qwen3-Next?**
- Excellent at free-form generation
- FAILS at structured output (contaminates JSON with thinking)
- FAILS at tool-calling (deliberates instead of acting)
- We keep it for RAG generation only, not for the intelligence pipeline

## Pipeline Phases

### Phase 1: Email Ingestion
- Reads `memory/email-log.json`
- Filters emails from last 24 hours
- Skips low-urgency automated emails
- Extracts facts and stores embeddings

### Phase 2: Task Ingestion
- Reads `memory/tasks.json`
- Finds recently completed tasks
- Ingests decisions and outcomes

### Phase 3: Contact Ingestion
- Reads `memory/contacts.json`
- Ingests contacts with recent activity

### Phase 4: Lifecycle Maintenance
- Runs via `maintenance.sh` at 3:00 AM (before intelligence pipeline)
- Decay scoring, archival, cleanup

### Phase 5: Quality Testing
- Runs sample queries against memory
- Llama evaluates relevance of results
- Logs scores to `rag-quality-log.json`

## Usage

### Manual Run
```bash
cd /home/philip/clawd/memory_system
source venv/bin/activate
python intelligence_pipeline.py
```

### Check Logs
```bash
# Intelligence pipeline logs
tail -100 /home/philip/clawd/memory_system/logs/intelligence.log

# Maintenance logs
tail -50 /home/philip/clawd/memory_system/maintenance.log

# Quality metrics
cat /home/philip/clawd/memory/rag-quality-log.json | jq '.runs[-1]'
```

## Key Files

| File | Purpose |
|------|---------|
| `maintenance.sh` | Mechanical maintenance (no LLM) |
| `intelligence_pipeline.py` | Smart ingestion + quality testing |
| `llm_client.py` | Ollama interface for Llama 3.2 |
| `rag.py` | RAG operations, search, stats |
| `logs/intelligence.log` | Pipeline run logs |
| `maintenance.log` | Maintenance run logs |

## NOT Using Moltbot Crons

The old "Memory Intelligence (Qwen Local)" Moltbot cron is **disabled**. It failed because:

1. Moltbot agentTurn sends tool schemas to the model
2. Model must produce structured tool calls
3. Qwen3-Next couldn't do this cleanly
4. We moved to Python scripts that orchestrate locally

All memory processing now runs via **system crontab**, not Moltbot.
