# CRM Tools Plugin for OpenClaw

An OpenClaw plugin that provides agent-callable tools for CRM operations and memory access. All tools proxy to the Bridge API service, which talks to the native CRM PostgreSQL tables.

## Overview

Tool names are identical to the MCP server tool names, so agent instructions work unchanged across both Claude Code (MCP) and OpenClaw (plugin) runtimes.

The plugin uses agent identity forwarding (`X-Agent-Id` header) for RBAC enforcement at the Bridge layer.

## Configuration

Set the `BRIDGE_URL` in your plugin config to point to your Bridge service:

```json
{
  "plugins": {
    "entries": {
      "crm-tools": {
        "enabled": true,
        "bridgeUrl": "http://127.0.0.1:9100"
      }
    }
  }
}
```

The Bridge service must be running and accessible at the configured URL.

## Memory Tools (8)

| Tool | Description |
|------|-------------|
| `search_memory` | Semantic search across stored facts |
| `store_memory` | Store new content, auto-extracts facts |
| `get_entity` | Look up entity and relationships in knowledge graph |
| `memory_stats` | Get fact counts, entity counts, active facts |
| `memory_block_read` | Read a named memory block (persona, user_profile, working_context, etc.) |
| `memory_block_write` | Write/replace content of a named memory block |
| `pipeline_status` | Get intelligence pipeline status and watermarks |
| `pipeline_trigger` | Trigger a pipeline tier on demand (1=ingest, 2=analysis, 3=deep) |

## CRM Tools

### Conversations

| Tool | Description |
|------|-------------|
| `list_conversations` | List conversations filtered by status (open, resolved, pending, snoozed) |
| `get_conversation` | Get detailed conversation info by ID |
| `list_messages` | List all messages in a conversation |
| `create_message` | Send a message in a conversation |
| `toggle_conversation_status` | Change conversation status (open, resolved, pending, snoozed) |

### People and Companies

| Tool | Description |
|------|-------------|
| `create_person` | Create a new person in CRM |
| `update_person` | Update person fields (never overwrites existing data) |
| `list_people` | List or search people |
| `create_note` | Create a CRM note |
| `update_company` | Update company fields |
| `merge_contacts` | Merge two duplicate people (primary absorbs secondary) |
| `merge_companies` | Merge two duplicate companies |

### Interaction Logging

| Tool | Description |
|------|-------------|
| `log_interaction` | Log an interaction to CRM, resolves contacts automatically |

### Health

| Tool | Description |
|------|-------------|
| `crm_health` | Check CRM system health (Bridge + Memory services) |

## Example Usage

```typescript
// In an OpenClaw agent session:

// Search for a contact
list_people({ search: "Jane" })

// Log an email interaction
log_interaction({
  contact_name: "Jane Smith",
  channel: "email",
  direction: "outgoing",
  content_summary: "Sent project update with Q1 deliverables"
})

// Search memory for context
search_memory({ query: "project timeline Q1", limit: 5 })

// Read working context
memory_block_read({ block_name: "working_context" })

// Trigger ingestion pipeline
pipeline_trigger({ tier: 1 })
```

## Architecture

```
OpenClaw Agent
  |
  v
crm-tools plugin (index.ts)
  |  HTTP proxy with X-Agent-Id header
  v
Bridge Service (:9100)
  |  RBAC enforcement + contact resolution
  v
PostgreSQL (crm_people, crm_companies, crm_notes, crm_tasks, ...)
Memory System (memory_facts, memory_entities, memory_relations, ...)
```

## Plugin File Structure

```
crm-tools/
  openclaw.plugin.json   # Plugin metadata and config schema
  index.ts               # Tool registrations and Bridge HTTP proxy
```

## Requirements

- Bridge service running at the configured URL
- PostgreSQL with CRM tables initialized
- Memory system tables initialized (for memory tools)
