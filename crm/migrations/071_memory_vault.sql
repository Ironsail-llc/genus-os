-- Migration 071: Knowledge Vault — verbatim reference store
--
-- A MIRIX-style Knowledge Vault for data the agent must recall EXACTLY:
-- contact numbers, account/case ids, addresses, bookmarks, and the rare
-- credential-like value. Unlike memory_facts (LLM-extracted, paraphrased),
-- vault entries preserve the original string byte-for-byte.
--
-- This is NOT the secrets vault (vault_secrets / robothor.vault). It is a
-- searchable, tenant-scoped memory store. Only the *caption* is embedded —
-- the value itself is never vectorized or logged. `high` sensitivity rows
-- are encrypted at rest (value_enc, AES-256-GCM via robothor.vault.crypto);
-- `low`/`medium` rows keep value_exact in plaintext.
--
-- Gated at the application layer by ROBOTHOR_RIP_12_ENABLED.

BEGIN;

CREATE TABLE IF NOT EXISTS memory_vault (
    id                BIGSERIAL PRIMARY KEY,
    tenant_id         TEXT NOT NULL DEFAULT 'default' REFERENCES crm_tenants(id),
    entry_type        TEXT NOT NULL,        -- contact_info | account_id | address | bookmark | credential | api_key
    caption           TEXT NOT NULL,        -- human description; the searchable surface
    value_exact       TEXT,                 -- verbatim value (NULL when encrypted)
    value_enc         BYTEA,                -- AES-256-GCM ciphertext (set iff sensitivity='high')
    sensitivity       TEXT NOT NULL DEFAULT 'medium',  -- low | medium | high
    source            TEXT,                 -- user_provided | crm | email | ...
    entity_id         INTEGER REFERENCES memory_entities(id) ON DELETE SET NULL,
    person_id         UUID REFERENCES crm_people(id) ON DELETE SET NULL,
    caption_embedding vector(1024),
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- exactly one of plaintext / ciphertext is present
    CONSTRAINT memory_vault_value_xor CHECK ((value_exact IS NULL) <> (value_enc IS NULL)),
    CONSTRAINT memory_vault_sensitivity CHECK (sensitivity IN ('low', 'medium', 'high'))
);

CREATE INDEX IF NOT EXISTS idx_vault_tenant
    ON memory_vault (tenant_id) WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_vault_type
    ON memory_vault (tenant_id, entry_type) WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_vault_caption_emb
    ON memory_vault USING hnsw (caption_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);
-- One entry per (tenant, type, caption) so re-stores upsert instead of duplicating.
CREATE UNIQUE INDEX IF NOT EXISTS idx_vault_dedup
    ON memory_vault (tenant_id, entry_type, md5(caption)) WHERE is_active;

-- Audit trail — every value read is logged (sensitivity carried for filtering).
CREATE TABLE IF NOT EXISTS vault_access_log (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   TEXT NOT NULL DEFAULT 'default' REFERENCES crm_tenants(id),
    vault_id    BIGINT NOT NULL,
    sensitivity TEXT NOT NULL,
    agent_id    TEXT,
    run_id      TEXT,
    accessed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_vault_access_entry
    ON vault_access_log (vault_id, accessed_at DESC);
CREATE INDEX IF NOT EXISTS idx_vault_access_tenant
    ON vault_access_log (tenant_id, accessed_at DESC);

COMMIT;
