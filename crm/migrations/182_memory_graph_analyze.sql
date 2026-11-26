-- Migration 182: planner statistics for the graph tables.
--
-- Migration 176 creates memory_mentions, memory_resolution_decisions and
-- memory_relation_evidence and copies every edge's supporting fact into the
-- evidence table. Until autovacuum analyzes them, the planner has no
-- statistics for them, and the graph walk (memory_activate, migration 178)
-- chose plans that took seconds per call on data it answers in tens of
-- milliseconds once analyzed.
--
-- ANALYZE samples a bounded number of rows per table, takes a SHARE UPDATE
-- EXCLUSIVE lock (writes and reads continue) and may run inside the
-- migration's transaction. It changes no data, so running it again is
-- harmless. A new migration rather than a line in 176, which is applied and
-- checksummed.

ANALYZE memory_relation_evidence;
ANALYZE memory_relations;
ANALYZE memory_mentions;
ANALYZE memory_resolution_decisions;
ANALYZE memory_entities;
