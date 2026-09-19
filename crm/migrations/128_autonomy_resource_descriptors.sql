BEGIN;

-- Names and provenance only. Resource values remain in authenticated ciphertext.
ALTER TABLE vault_resources ADD COLUMN IF NOT EXISTS descriptor JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMIT;
