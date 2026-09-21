-- Request identities survive worker replacement; uncertain usage remains reserved.
CREATE TABLE IF NOT EXISTS pursuit_goal_provider_reservations (
    tenant_id TEXT NOT NULL,
    attempt_id UUID NOT NULL,
    call_id TEXT NOT NULL,
    reserved BIGINT NOT NULL CHECK (reserved > 0),
    actual BIGINT CHECK (actual >= 0),
    PRIMARY KEY (tenant_id, attempt_id, call_id),
    FOREIGN KEY (tenant_id, attempt_id) REFERENCES pursuit_goal_attempts(tenant_id, id)
);
ALTER TABLE pursuit_goal_provider_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE pursuit_goal_provider_reservations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON pursuit_goal_provider_reservations;
CREATE POLICY tenant_isolation ON pursuit_goal_provider_reservations USING (
    current_setting('app.tenant_id',true) IS NULL OR
    current_setting('app.tenant_id',true)='' OR tenant_id=current_setting('app.tenant_id',true)
) WITH CHECK (
    current_setting('app.tenant_id',true) IS NULL OR
    current_setting('app.tenant_id',true)='' OR tenant_id=current_setting('app.tenant_id',true)
);
