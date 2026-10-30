# Knowledge Base

The knowledge base holds **curated reference documents** — policies, product
documentation, playbooks — in named **collections**, and lets an agent search
one collection at a time. It is a core primitive: a plugin cannot ship a
migration, so the tables and tools live in the platform and any plugin or agent
can fill a collection and search it.

It is not memory. [Memory](memory-system.md) is what the agent learned and
remembered on its own (`search_memory`, `store_memory`); the knowledge base is
material someone chose to put there, kept whole, versioned, and scoped to a
collection.

## Collections

A collection is named `<plugin>/<topic>`: lowercase segments of letters,
digits, `_`, `.` and `-`, joined by `/`, at most 128 characters. For example
`helpdesk/policies` or `sales/playbooks`. The first segment names the plugin
(or the team) that owns the collection, so two plugins never write into each
other's documents by accident. A collection exists as soon as it holds a
document; there is no separate create step.

Everything is per tenant: the tools take the tenant from the run, and a
collection in one tenant is invisible to another.

## Documents

A document is identified by `(collection, doc_id)` and carries a title, its
text, an optional `source_uri` and metadata. On upsert the text is split into
windows of about 400 words that overlap by 40, and every window is embedded
with the same model as memory facts (1024 dimensions).

| Upsert result | When |
|---|---|
| `created` | No document with that `doc_id` in the collection. Version 1. |
| `unchanged` | Title, text, `source_uri` and metadata hash to the stored SHA-256. Nothing is embedded or written. |
| `updated` | Anything changed. Every chunk is replaced and the version goes up by one. |

Re-sending a whole collection on every sync is therefore cheap: only the
documents that changed are re-embedded. An embedding failure fails the upsert;
a document is never stored half-indexed.

## Search

`kb_search` runs the same hybrid pipeline as memory search: a vector leg
(cosine similarity) and a keyword leg (PostgreSQL full-text, BM25 `ts_rank`),
fused by reciprocal rank fusion, `1/(60 + rank)` summed over the legs. Each
hit is one chunk: `doc_id`, `title`, `chunk_ix`, `text`, `score` (the fused
score) and `source_uri`. If the embedding service is down, the keyword leg
carries the search and each hit is marked `degraded`.

## Tools

| Tool | What it does |
|---|---|
| `kb_search` | `collection`, `query`, `limit` (default 5, at most 50). Read-only. |
| `kb_upsert_document` | `collection`, `doc_id`, `text`, and optionally `title` and `source_uri`. Returns `status`, `version`, `chunks`. Text over 200,000 characters is refused with `document_too_large` before anything is embedded: split it across several `doc_id`s. |
| `kb_delete_document` | `collection`, `doc_id`. Returns `deleted`: false when there was no such document. |
| `kb_list_collections` | Every collection in the tenant with its document and chunk counts and when it last changed. Read-only. |

The two writes are refused in a benchmark run. Use `kb_search` for reference
documents scoped to a collection, and `search_memory` for the agent's own
remembered facts. See [Tools](TOOLS.md#knowledge-base-kb_).

## From Python

A plugin can fill a collection without going through a tool call:

```python
from robothor.knowledge import search, store

await store.upsert_document(tenant_id, "helpdesk/policies", "refunds", "Refunds", text)
hits = await search.search(tenant_id, "helpdesk/policies", "how long do refunds take")
```

Both modules run their database work off the event loop. The tables are
`kb_documents` and `kb_chunks` (migration 155), with the same HNSW and GIN
index shapes as `memory_facts` and the standard tenant row-level-security
policy.
