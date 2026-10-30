-- Migration 155: knowledge base (robothor/knowledge/).
--
-- kb_documents: one row per (tenant, collection, doc_id) -- a curated
-- reference document an agent or plugin stored with kb_upsert_document.
-- Collections are named <plugin>/<topic>. sha256 covers the title, text,
-- source_uri and metadata: an upsert whose hash matches is skipped, and any
-- other upsert replaces every chunk and adds one to version.
--
-- kb_chunks: the document split into overlapping windows (chunk_ix from 0),
-- each embedded with the same model and width as memory_facts, plus a
-- generated tsvector so search can fuse a vector leg and a BM25 leg with
-- reciprocal rank fusion, as memory search does. Chunks go with their
-- document (ON DELETE CASCADE).

CREATE TABLE IF NOT EXISTS kb_documents (
    tenant_id TEXT NOT NULL,
    collection TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    source_uri TEXT NOT NULL DEFAULT '',
    version INT NOT NULL DEFAULT 1,
    sha256 TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, collection, doc_id)
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    tenant_id TEXT NOT NULL,
    collection TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    chunk_ix INT NOT NULL,
    text TEXT NOT NULL,
    embedding vector(1024),
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    PRIMARY KEY (tenant_id, collection, doc_id, chunk_ix),
    FOREIGN KEY (tenant_id, collection, doc_id)
        REFERENCES kb_documents (tenant_id, collection, doc_id) ON DELETE CASCADE
);

-- Same index shapes as memory_facts (migration 023).
CREATE INDEX IF NOT EXISTS idx_kb_chunks_tsv ON kb_chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_embedding ON kb_chunks
  USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=200);

ALTER TABLE kb_documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_documents FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON kb_documents;
CREATE POLICY tenant_isolation ON kb_documents
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

ALTER TABLE kb_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE kb_chunks FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON kb_chunks;
CREATE POLICY tenant_isolation ON kb_chunks
    USING (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL OR current_setting('app.tenant_id', true) = ''
           OR tenant_id = current_setting('app.tenant_id', true));

-- Rollback:
--   DROP TABLE IF EXISTS kb_chunks;
--   DROP TABLE IF EXISTS kb_documents;
