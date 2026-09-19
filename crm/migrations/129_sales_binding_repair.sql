-- Preserve unknown attribution after the final practice is reassigned away.
ALTER TABLE sales_prospects ADD COLUMN IF NOT EXISTS business_attribution_detached BOOLEAN NOT NULL DEFAULT FALSE;
-- Review generations prevent a stale A->B request after a later A->B->A repair.
ALTER TABLE sales_customer_bindings ADD COLUMN IF NOT EXISTS binding_version INTEGER NOT NULL DEFAULT 1 CHECK(binding_version>0);
