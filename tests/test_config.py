"""Tests for robothor.config — centralized configuration."""


import pytest

from robothor.config import (
    Config,
    DatabaseConfig,
    OllamaConfig,
    RedisConfig,
    get_config,
    reset_config,
)


@pytest.fixture(autouse=True)
def clean_config():
    """Reset config singleton between tests."""
    reset_config()
    yield
    reset_config()


class TestDatabaseConfig:
    def test_defaults(self):
        db = DatabaseConfig()
        assert db.host == ""
        assert db.port == 5432
        assert db.name == "robothor_memory"

    def test_dsn(self):
        db = DatabaseConfig(host="db.example.com", port=5433, name="test", user="tester")
        assert "dbname=test" in db.dsn
        assert "host=db.example.com" in db.dsn
        assert "port=5433" in db.dsn
        assert "user=tester" in db.dsn

    def test_dsn_no_password(self):
        db = DatabaseConfig(password="")
        assert "password" not in db.dsn

    def test_dsn_with_password(self):
        db = DatabaseConfig(password="secret")
        assert "password=secret" in db.dsn

    def test_dict(self):
        db = DatabaseConfig(host="localhost", port=5432, name="test", user="u")
        d = db.dict
        assert d["dbname"] == "test"
        assert d["host"] == "localhost"
        assert d["port"] == 5432
        assert d["user"] == "u"

    def test_frozen(self):
        db = DatabaseConfig()
        with pytest.raises(AttributeError):
            db.host = "other"  # type: ignore[misc]


class TestRedisConfig:
    def test_url_no_password(self):
        r = RedisConfig()
        assert r.url == "redis://127.0.0.1:6379/0"

    def test_url_with_password(self):
        r = RedisConfig(password="pass123")
        assert r.url == "redis://:pass123@127.0.0.1:6379/0"


class TestOllamaConfig:
    def test_base_url(self):
        o = OllamaConfig()
        assert o.base_url == "http://127.0.0.1:11434"

    def test_custom_host(self):
        o = OllamaConfig(host="gpu.local", port=11435)
        assert o.base_url == "http://gpu.local:11435"


class TestConfig:
    def test_bridge_url(self):
        c = Config()
        assert c.bridge_url == "http://127.0.0.1:9100"

    def test_custom_ports(self):
        c = Config(bridge_port=9200, orchestrator_port=9199)
        assert c.bridge_url == "http://127.0.0.1:9200"
        assert c.orchestrator_url == "http://127.0.0.1:9199"


class TestGetConfig:
    def test_returns_config(self):
        cfg = get_config()
        assert isinstance(cfg, Config)

    def test_singleton(self):
        cfg1 = get_config()
        cfg2 = get_config()
        assert cfg1 is cfg2

    def test_env_override_db(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_DB_HOST", "db.remote.com")
        monkeypatch.setenv("ROBOTHOR_DB_PORT", "5433")
        monkeypatch.setenv("ROBOTHOR_DB_NAME", "custom_db")
        cfg = get_config()
        assert cfg.db.host == "db.remote.com"
        assert cfg.db.port == 5433
        assert cfg.db.name == "custom_db"

    def test_env_override_redis(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_REDIS_HOST", "redis.remote.com")
        monkeypatch.setenv("ROBOTHOR_REDIS_PORT", "6380")
        cfg = get_config()
        assert cfg.redis.host == "redis.remote.com"
        assert cfg.redis.port == 6380

    def test_env_override_ollama(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_OLLAMA_HOST", "gpu.local")
        monkeypatch.setenv("ROBOTHOR_EMBEDDING_MODEL", "custom-embed:latest")
        cfg = get_config()
        assert cfg.ollama.host == "gpu.local"
        assert cfg.ollama.embedding_model == "custom-embed:latest"

    def test_env_override_ports(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_BRIDGE_PORT", "9200")
        cfg = get_config()
        assert cfg.bridge_port == 9200

    def test_env_override_workspace(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_WORKSPACE", "/opt/robothor")
        cfg = get_config()
        assert str(cfg.workspace) == "/opt/robothor"

    def test_identity_defaults(self):
        cfg = get_config()
        assert cfg.owner_name == "there"
        assert cfg.ai_name == "Robothor"

    def test_identity_env_override(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_OWNER_NAME", "Alice")
        monkeypatch.setenv("ROBOTHOR_AI_NAME", "Jarvis")
        cfg = get_config()
        assert cfg.owner_name == "Alice"
        assert cfg.ai_name == "Jarvis"
