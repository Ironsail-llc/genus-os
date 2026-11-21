-- Migration 169: shared threads ("rooms").
--
-- A room is a company-owned sub-conversation: its own chat session
-- (``agent:{agent_id}:room:{room_id}``), a definition kept as numbered
-- revisions, the users who can access it, the editions a report publishes, and
-- the CRM tasks linked to it. See docs/SYSTEM_ARCHITECTURE.md ("Shared threads").
--
-- Every table is tenant-keyed with forced row-level security. Child tables
-- reference ``rooms(id)`` by a single-column key (room ids are UUIDs, unique
-- platform-wide) rather than a composite ``(tenant_id, room_id)`` one:
-- ``genus tenant move-workload`` re-tenants moved rows in place, which a
-- composite foreign key without ON UPDATE CASCADE would refuse. User ids are
-- plain UUIDs with no foreign key, so a membership never blocks an account move.
--
-- Idempotent: every statement can run again.

CREATE TABLE IF NOT EXISTS rooms (
    tenant_id            TEXT NOT NULL,
    id                   UUID NOT NULL,
    kind                 TEXT NOT NULL CHECK (kind IN ('report', 'agent', 'task')),
    title                TEXT NOT NULL,
    agent_id             TEXT NOT NULL,
    session_key          TEXT NOT NULL UNIQUE,
    created_by           UUID NOT NULL,
    run_as_user_id       UUID,
    instructions         TEXT NOT NULL DEFAULT '',
    settings             JSONB NOT NULL DEFAULT '{}'::jsonb,
    task_id              UUID,
    paused               BOOLEAN NOT NULL DEFAULT FALSE,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    rev                  INTEGER NOT NULL DEFAULT 1,
    last_activity_at     TIMESTAMPTZ,
    last_message_id      BIGINT,
    archived_at          TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (id),
    UNIQUE (tenant_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_rooms_activity
    ON rooms (tenant_id, last_activity_at DESC NULLS LAST);

CREATE TABLE IF NOT EXISTS room_members (
    tenant_id             TEXT NOT NULL,
    room_id               UUID NOT NULL REFERENCES rooms (id) ON DELETE CASCADE,
    user_id               UUID NOT NULL,
    assigned              BOOLEAN NOT NULL DEFAULT FALSE,
    added_by              UUID,
    last_read_message_id  BIGINT,
    last_read_edition_seq INTEGER,
    notify                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, room_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_room_members_user ON room_members (tenant_id, user_id);

CREATE TABLE IF NOT EXISTS room_revisions (
    tenant_id  TEXT NOT NULL,
    room_id    UUID NOT NULL REFERENCES rooms (id) ON DELETE CASCADE,
    rev        INTEGER NOT NULL,
    changed_by UUID,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    definition JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, room_id, rev)
);

CREATE TABLE IF NOT EXISTS room_editions (
    tenant_id       TEXT NOT NULL,
    id              UUID NOT NULL,
    room_id         UUID NOT NULL REFERENCES rooms (id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'edition' CHECK (kind IN ('edition', 'dashboard')),
    status          TEXT NOT NULL DEFAULT 'building'
                    CHECK (status IN ('building', 'published', 'failed')),
    preview         BOOLEAN NOT NULL DEFAULT FALSE,
    period_start    DATE,
    period_end      DATE,
    grain           TEXT CHECK (grain IN ('day', 'week', 'month', 'custom')),
    revision        INTEGER NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',
    blocks          JSONB NOT NULL DEFAULT '[]'::jsonb,
    suggestions     JSONB NOT NULL DEFAULT '[]'::jsonb,
    provenance      JSONB NOT NULL DEFAULT '{}'::jsonb,
    error           TEXT,
    run_id          TEXT,
    chat_message_id BIGINT,
    requested_by    UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (id),
    UNIQUE (tenant_id, room_id, seq)
);
-- One build per period at a time: concurrent requests for the same period
-- share the row being built. Previews are per requester and never deduplicated.
CREATE UNIQUE INDEX IF NOT EXISTS uq_room_editions_building_period
    ON room_editions (tenant_id, room_id, grain, period_start, period_end)
    WHERE status = 'building' AND NOT preview;
CREATE INDEX IF NOT EXISTS idx_room_editions_period
    ON room_editions (tenant_id, room_id, grain, period_start, period_end);

CREATE TABLE IF NOT EXISTS room_tasks (
    tenant_id      TEXT NOT NULL,
    room_id        UUID NOT NULL REFERENCES rooms (id) ON DELETE CASCADE,
    task_id        UUID NOT NULL,
    edition_id     UUID REFERENCES room_editions (id) ON DELETE SET NULL,
    origin         TEXT NOT NULL DEFAULT 'linked'
                   CHECK (origin IN ('subject', 'suggestion', 'linked')),
    suggestion_key TEXT,
    accepted_by    UUID,
    dismissed_by   UUID,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, room_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_room_tasks_task ON room_tasks (tenant_id, task_id);

-- A room's schedule, and the user whose permissions a scheduled run uses.
ALTER TABLE user_cronjobs ADD COLUMN IF NOT EXISTS room_id UUID;
ALTER TABLE user_cronjobs ADD COLUMN IF NOT EXISTS run_as_user_id UUID;
CREATE INDEX IF NOT EXISTS idx_user_cronjobs_room
    ON user_cronjobs (tenant_id, room_id) WHERE room_id IS NOT NULL;

-- Which run proposed an action, for whom, and from which room (member approval).
ALTER TABLE operation_actions ADD COLUMN IF NOT EXISTS proposed_by_run_id TEXT;
ALTER TABLE operation_actions ADD COLUMN IF NOT EXISTS proposed_for_user_id UUID;
ALTER TABLE operation_actions ADD COLUMN IF NOT EXISTS room_id UUID;
CREATE INDEX IF NOT EXISTS idx_operation_actions_room
    ON operation_actions (tenant_id, room_id) WHERE room_id IS NOT NULL;

-- The platform's tenant_isolation shape (081, 164): a connection bound to a
-- tenant sees only its rows; an unbound one (the migration runner, an RLS-off
-- install, the operator moves' read_every_tenant_in_transaction) sees all.
ALTER TABLE rooms ENABLE ROW LEVEL SECURITY;
ALTER TABLE rooms FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON rooms;
CREATE POLICY tenant_isolation ON rooms
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE room_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE room_members FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON room_members;
CREATE POLICY tenant_isolation ON room_members
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE room_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE room_revisions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON room_revisions;
CREATE POLICY tenant_isolation ON room_revisions
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE room_editions ENABLE ROW LEVEL SECURITY;
ALTER TABLE room_editions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON room_editions;
CREATE POLICY tenant_isolation ON room_editions
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE room_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE room_tasks FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON room_tasks;
CREATE POLICY tenant_isolation ON room_tasks
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

-- Role seed. owner and admin already allow '*'. member is read-only by default
-- (migration 088) and user allows '*': both get the room tools and are denied
-- the admin ones (the more specific pattern wins, robothor/engine/permissions.py).
-- viewer may list and read.
INSERT INTO role_permissions (tenant_id, role, tool_pattern, access) VALUES
    ('__default__', 'member', 'room_*', 'allow'),
    ('__default__', 'member', 'room_admin_*', 'deny'),
    ('__default__', 'user', 'room_*', 'allow'),
    ('__default__', 'user', 'room_admin_*', 'deny'),
    ('__default__', 'viewer', 'room_list', 'allow'),
    ('__default__', 'viewer', 'room_read', 'allow')
ON CONFLICT (tenant_id, role, tool_pattern) DO NOTHING;
