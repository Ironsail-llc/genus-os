-- Migration 010: Health tables for Garmin biometric data
-- Migrated from ~/garmin-sync/ SQLite to PostgreSQL
-- No tenant_id — personal biometric data, single-user

-- Time-series tables (Unix seconds PK)

CREATE TABLE IF NOT EXISTS health_heart_rate (
    timestamp BIGINT PRIMARY KEY,
    heart_rate INTEGER NOT NULL,
    source TEXT DEFAULT 'monitoring'
);
CREATE INDEX IF NOT EXISTS idx_health_hr_ts ON health_heart_rate(timestamp);

CREATE TABLE IF NOT EXISTS health_stress (
    timestamp BIGINT PRIMARY KEY,
    stress_level INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_stress_ts ON health_stress(timestamp);

CREATE TABLE IF NOT EXISTS health_body_battery (
    timestamp BIGINT PRIMARY KEY,
    level INTEGER NOT NULL,
    charged INTEGER,
    drained INTEGER
);
CREATE INDEX IF NOT EXISTS idx_health_bb_ts ON health_body_battery(timestamp);

CREATE TABLE IF NOT EXISTS health_spo2 (
    timestamp BIGINT PRIMARY KEY,
    spo2_value INTEGER NOT NULL,
    reading_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_health_spo2_ts ON health_spo2(timestamp);

CREATE TABLE IF NOT EXISTS health_respiration (
    timestamp BIGINT PRIMARY KEY,
    respiration_rate REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_resp_ts ON health_respiration(timestamp);

CREATE TABLE IF NOT EXISTS health_hrv (
    timestamp BIGINT PRIMARY KEY,
    hrv_value REAL,
    reading_type TEXT,
    status TEXT
);
CREATE INDEX IF NOT EXISTS idx_health_hrv_ts ON health_hrv(timestamp);

-- Daily tables (DATE PK)

CREATE TABLE IF NOT EXISTS health_sleep (
    date DATE PRIMARY KEY,
    start_timestamp BIGINT,
    end_timestamp BIGINT,
    total_sleep_seconds INTEGER,
    deep_sleep_seconds INTEGER,
    light_sleep_seconds INTEGER,
    rem_sleep_seconds INTEGER,
    awake_seconds INTEGER,
    score INTEGER,
    quality TEXT,
    raw_data JSONB
);

CREATE TABLE IF NOT EXISTS health_steps (
    date DATE PRIMARY KEY,
    total_steps INTEGER NOT NULL,
    goal INTEGER,
    distance_meters REAL,
    calories INTEGER,
    timestamp BIGINT
);

CREATE TABLE IF NOT EXISTS health_resting_heart_rate (
    date DATE PRIMARY KEY,
    resting_hr INTEGER NOT NULL,
    timestamp BIGINT
);

CREATE TABLE IF NOT EXISTS health_daily_summary (
    date DATE PRIMARY KEY,
    calories_total INTEGER,
    calories_active INTEGER,
    calories_bmr INTEGER,
    floors_climbed INTEGER,
    intensity_minutes INTEGER,
    raw_data JSONB
);

CREATE TABLE IF NOT EXISTS health_training_status (
    date DATE PRIMARY KEY,
    training_status TEXT,
    training_status_phrase TEXT,
    vo2max_running REAL,
    vo2max_cycling REAL,
    training_load_7_day INTEGER,
    training_load_28_day INTEGER,
    recovery_time_hours INTEGER,
    raw_data JSONB
);

-- Activities (Garmin activity_id PK)

CREATE TABLE IF NOT EXISTS health_activities (
    activity_id BIGINT PRIMARY KEY,
    name TEXT,
    activity_type TEXT,
    start_timestamp BIGINT NOT NULL,
    duration_seconds INTEGER,
    distance_meters REAL,
    calories INTEGER,
    avg_heart_rate INTEGER,
    max_heart_rate INTEGER,
    avg_pace REAL,
    elevation_gain REAL,
    vo2max REAL,
    training_effect_aerobic REAL,
    training_effect_anaerobic REAL,
    training_load INTEGER,
    raw_data JSONB
);
CREATE INDEX IF NOT EXISTS idx_health_act_start ON health_activities(start_timestamp);

-- Sync log

CREATE TABLE IF NOT EXISTS health_sync_log (
    id SERIAL PRIMARY KEY,
    sync_timestamp BIGINT NOT NULL,
    metric_type TEXT NOT NULL,
    records_synced INTEGER,
    status TEXT,
    error_message TEXT
);
