-- Seed the historical memory tenant before migration 033 adds foreign keys
-- whose legacy default is ``robothor-primary``.  Fresh consolidated installs
-- otherwise fail as soon as a pre-existing memory-block row is backfilled.

INSERT INTO crm_tenants (id, display_name)
VALUES ('robothor-primary', 'Primary')
ON CONFLICT (id) DO NOTHING;
