-- Remote numeric IDs are meaningful only inside the original provider account.
ALTER TABLE sales_prospects ADD COLUMN IF NOT EXISTS pipedrive_account_scope TEXT;
