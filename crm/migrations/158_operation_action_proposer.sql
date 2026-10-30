-- Migration 158: who proposed an operation action.
--
-- A policy actor (policy:<plugin>:<version>) may decide only the actions its
-- own plugin proposed, so the proposer is recorded on the row. Rows proposed
-- before this column, and proposals that name no proposer, stay NULL and can
-- be decided only by an operator.
ALTER TABLE operation_actions ADD COLUMN IF NOT EXISTS proposed_by TEXT;
