"""
Regression guard: os.getenv("KEY", "default") only falls back when the key
is entirely absent from .env, NOT when it's present but left blank (e.g.
"DISCORD_GUILD_ID=" from a freshly-copied .env.example) - that crashed the
bot at import time with a ValueError instead of using the documented
default. config.py fixed this by switching every int()/float() env var to
os.getenv("KEY") or "default"; this test reloads the module with each of
those vars set to an empty string and makes sure it still imports cleanly
with the right defaults, so nobody accidentally reverts to the two-arg form.
"""
import importlib

import config


def test_blank_numeric_env_vars_fall_back_to_defaults(monkeypatch):
    for key in (
        "DISCORD_GUILD_ID", "OLLAMA_NUM_CTX", "AI_TIMEOUT_SECONDS",
        "BACKUP_INTERVAL_HOURS", "BACKUP_KEEP_COUNT", "BACKUP_MAX_AGE_DAYS",
        "BUYER_DATA_RETENTION_DAYS",
    ):
        monkeypatch.setenv(key, "")

    reloaded = importlib.reload(config)
    try:
        assert reloaded.GUILD_ID == 0
        assert reloaded.OLLAMA_NUM_CTX == 4096
        assert reloaded.AI_TIMEOUT_SECONDS == 90.0
        assert reloaded.BACKUP_INTERVAL_HOURS == 24
        assert reloaded.BACKUP_KEEP_COUNT == 14
        assert reloaded.BACKUP_MAX_AGE_DAYS == 14
        assert reloaded.BUYER_DATA_RETENTION_DAYS == 90
    finally:
        # Other test modules import the shared `config` singleton - leave
        # it holding the real (non-blank) test settings from conftest.py
        # rather than these blanked-out values.
        for key in (
            "DISCORD_GUILD_ID", "OLLAMA_NUM_CTX", "AI_TIMEOUT_SECONDS",
            "BACKUP_INTERVAL_HOURS", "BACKUP_KEEP_COUNT", "BACKUP_MAX_AGE_DAYS",
            "BUYER_DATA_RETENTION_DAYS",
        ):
            monkeypatch.delenv(key, raising=False)
        importlib.reload(config)
