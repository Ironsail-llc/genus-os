"""Regression coverage for deployment-controlled PostgreSQL TLS."""

from robothor.config import DatabaseConfig, get_config, reset_config


def test_database_config_propagates_sslmode_to_libpq():
    config = DatabaseConfig(host="db.example.com", ssl_mode="verify-full")

    assert config.dict["sslmode"] == "verify-full"
    assert "sslmode=verify-full" in config.dsn
    assert config.url.endswith("?sslmode=verify-full")


def test_database_sslmode_loads_from_environment(monkeypatch):
    reset_config()
    monkeypatch.setenv("ROBOTHOR_DB_SSLMODE", "verify-full")
    try:
        assert get_config().db.ssl_mode == "verify-full"
    finally:
        reset_config()
