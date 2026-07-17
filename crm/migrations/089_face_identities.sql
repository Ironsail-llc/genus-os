-- Migration 089: face_identities — link vision face-recognition labels to
-- the person identity graph (Task 7, Unified Identity Context).
--
-- The vision service (robothor/vision/service.py, port 8600, hand-rolled
-- HTTP) recognizes enrolled faces by a plain string label (InsightFace
-- embeddings persisted to FACE_DATA_DIR/enrolled_faces.json) and has NO
-- database access — and stays that way (root CLAUDE.md: platform code must
-- never grow instance-specific coupling, and the vision process is kept
-- DB-free by design). This table is the engine-side join: face_label ->
-- person_id, written by the engine's vision tool handlers
-- (robothor/engine/tools/handlers/vision.py: enroll_face,
-- enroll_face_from_image, unenroll_face) and the `robothor user link-face`
-- CLI (robothor/cli/user.py), and read by
-- robothor/identity/resolvers.py::_resolve_vision (which already ships
-- probing for this table via to_regclass and degrades to None gracefully on
-- environments that haven't applied this migration yet).
--
-- display_name is stored redundantly rather than always re-derived from
-- person_id: an enrollment can be unlinked (person_id NULL) with only a
-- free-text display_name, and _resolve_vision's join falls back to
-- crm_people's name only when this column is empty.
--
-- user_account_id is a secondary optional link for the rarer case where the
-- enrolled human also has an SSO/webchat login (user_accounts, mig 071) —
-- not populated by Task 7's writers today, but present so a later phase can
-- wire it without another migration.
--
-- Rollback: DROP TABLE IF EXISTS face_identities;

CREATE TABLE IF NOT EXISTS face_identities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       TEXT NOT NULL REFERENCES crm_tenants(id),
    face_label      TEXT NOT NULL,
    person_id       UUID REFERENCES crm_people(id) ON DELETE SET NULL,
    user_account_id UUID REFERENCES user_accounts(id) ON DELETE SET NULL,
    display_name    TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, face_label)
);

CREATE INDEX IF NOT EXISTS idx_face_identities_tenant ON face_identities (tenant_id);

CREATE INDEX IF NOT EXISTS idx_face_identities_person ON face_identities (person_id)
    WHERE person_id IS NOT NULL;
