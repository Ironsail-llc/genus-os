-- Migration 179: memory graph keys — one mention per fact and surface form, and
-- the context a deferred memory write needs.
--
-- WHY
--   * memory_mentions had no unique key on (fact_id, lower(surface_form)), so
--     re-ingesting the same fact could only be kept idempotent in code (a
--     guarded INSERT ... WHERE NOT EXISTS under an advisory lock). The write
--     path now writes mentions for every stored fact, from several writers at
--     once; the database enforces it instead and writers use
--     ON CONFLICT DO NOTHING.
--   * memory_write_jobs replays a deferred store_memory call later. The write
--     path needs the call's origin, scope and conversation to store it the way
--     it would have been stored at once, so the job row carries them
--     (write_context, the JSON of robothor.memory.write_path.WriteContext).
--
-- NOT DONE HERE, ON PURPOSE: the unique key on memory_entities
-- (tenant_id, name, entity_type) stays as it is. Two CRM people who share a name
-- would each want a node with that plain name, which the key forbids, so the
-- identity code names the second one "<name> #<crm id prefix>" and keeps the
-- plain name as an alias. Making the key partial (unlinked rows only) and adding
-- unique keys on the CRM links would remove that workaround, but an older
-- release's upsert_entity runs
--     INSERT ... ON CONFLICT (tenant_id, name, entity_type) DO UPDATE ...
--   and Postgres refuses that statement outright ("there is no unique or
--   exclusion constraint matching the ON CONFLICT specification") when only a
--   partial index exists. An older release must run unchanged against the
--   migrated database, so the key stays and so does the workaround.
--
-- SAFETY
--   Additive. The unique index is built on memory_mentions, a table created by
--   the graph foundation migration and filled only by the identity backfill and
--   the write path, so it is small; exact duplicates (same fact, same surface
--   form ignoring case) are removed first, keeping the oldest row, so the build
--   cannot fail on them. The new memory_write_jobs column is nullable with no
--   default: a metadata-only change, and a job written by an older release (no
--   context) is replayed as a shared, agent-generated write — what it was.
--
-- IDEMPOTENT: IF NOT EXISTS everywhere; the duplicate cleanup finds nothing the
-- second time. Safe to re-run under a different number.
--
-- Rollback:
--   DROP INDEX IF EXISTS uq_memory_mentions_fact_surface;
--   ALTER TABLE memory_write_jobs DROP COLUMN IF EXISTS write_context;

DELETE FROM memory_mentions m
 USING memory_mentions keep
 WHERE keep.fact_id = m.fact_id
   AND lower(keep.surface_form) = lower(m.surface_form)
   AND keep.id < m.id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_mentions_fact_surface
    ON memory_mentions (fact_id, lower(surface_form));

ALTER TABLE memory_write_jobs
    ADD COLUMN IF NOT EXISTS write_context JSONB;
