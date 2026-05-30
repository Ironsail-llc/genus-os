# Runbook: pgvector 0.6.0 → 0.8.x (HNSW iterative scan)

**Status:** ✅ Executed 2026-05-30 — pgvector upgraded **0.6.0 → 0.8.2** via PGDG
(`ALTER EXTENSION vector UPDATE`), `iterative_scan` wired into all 9 vector-search
sites behind `MEMORY_HNSW_ITERATIVE` via the shared `apply_hnsw_session` helper
(`robothor/memory/vector_tuning.py`). No reindex, no PG restart. 809M backup at
`/var/backups/`.
**Why:** pgvector 0.6.0 HNSW *post-filters* (`WHERE` applied after the index
returns its `ef_search` nearest), which collapsed `memory_facts` semantic recall
to ~1 hit at scale (fixed for now with partial-active indexes, migrations 073/074).
pgvector **0.8** adds `hnsw.iterative_scan` — the index keeps fetching until
`LIMIT` is satisfied or `hnsw.max_scan_tuples` is hit — the *proper* fix for
filtered vector search, and resilient as the corpus grows / multi-tenant filters
tighten.

## Current state (measured 2026-05-30)
- PostgreSQL **16.11** (Ubuntu 24.04), pgvector **0.6.0** via apt
  `postgresql-16-pgvector 0.6.0-1`. `pg_available_extension_versions` lists only
  `0.6.0` → the 0.8 shared library is **not installed yet**.
- 13 HNSW indexes, ~982 MB total (`memory_facts` dominates).
- Partial-active HNSW indexes already in place (073/074); `hnsw.ef_search=100`
  and the ranking blend are deployed in `search_facts`.

## Risk profile: LOW
- `iterative_scan` is **opt-in** (GUC default `off`). After the upgrade, default
  query behavior is unchanged until we explicitly enable it — so the upgrade
  itself can't regress anything.
- Existing HNSW indexes remain valid across 0.6→0.8; **no reindex required** for
  iterative scan. (Optional REINDEX only if we later want 0.8 build improvements.)
- `ALTER EXTENSION … UPDATE` and installing the new `.so` do **not** require a
  PostgreSQL restart; new connections pick up the new library.

## Procedure

### 0. Backup (do first)
```bash
sudo -u postgres pg_dump robothor_memory | gzip > /var/backups/robothor_memory-$(date +%F).sql.gz
```

### 1. Install pgvector ≥0.8 for PG16 — pick ONE

**A. PGDG apt repo (recommended — clean, tracked):**
```bash
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
  https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list
sudo apt update
sudo apt install --only-upgrade postgresql-16-pgvector   # PGDG ships a newer build
```

**B. Build from source (no repo dependency):**
```bash
sudo apt install postgresql-server-dev-16 build-essential git
git clone --branch v0.8.0 https://github.com/pgvector/pgvector.git /tmp/pgvector
cd /tmp/pgvector && make && sudo make install
```

### 2. Update the extension in the DB
```bash
psql robothor_memory -c "SELECT default_version FROM pg_available_extensions WHERE name='vector';"  # expect 0.8.x
psql robothor_memory -c "ALTER EXTENSION vector UPDATE;"
psql robothor_memory -c "SELECT extversion FROM pg_extension WHERE extname='vector';"               # expect 0.8.x
```

### 3. Enable iterative scan in the memory retriever
In `robothor/memory/facts.py` `search_facts` (next to the existing `SET LOCAL
hnsw.ef_search`), add — gated by an env flag for safe rollout:
```python
# pgvector >= 0.8 only; relaxed_order = better recall, approximate ordering
with contextlib.suppress(Exception):
    cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
    cur.execute("SET LOCAL hnsw.max_scan_tuples = 20000")
```
Add a `_iterative_scan_enabled()` helper reading `MEMORY_HNSW_ITERATIVE` (default
off until 0.8 is confirmed live), mirroring `_hnsw_ef_search()`. Apply the same
to `vault.search_vault`, `intents.search_intents`, `episodes`, `insights`.

### 4. Verify
```bash
# the query that returned 1 before should return a full 30 even WITHOUT the
# partial index doing the work — iterative scan handles the filter natively
robothor memory-eval        # expect no regression
```
Spot-check a filtered vector query with `EXPLAIN` to confirm the iterative scan
node appears, and that recall holds with `ef_search` back at a normal value.

### 5. Rollback
- Extension downgrade is not supported; rollback = restore the dump from step 0.
- But practically: leave `MEMORY_HNSW_ITERATIVE` off and nothing changes vs today.
  The partial indexes (073/074) remain the safety net regardless of version.

## After the upgrade
- The partial-active indexes can stay (still help) — but iterative scan means we
  no longer *depend* on them, and could drop the redundant full indexes to cut
  write amplification.
- Revisit `hnsw.ef_search` (could lower back toward default with iterative scan on).
