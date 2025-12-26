import os
import pytest
from doorman_agent.config import load_config
from doorman_agent.models import Config

def test_load_config_happy_path(monkeypatch):
    """
    Test load_config with default values (happy path).
    Ensures that when no config file or env vars are present,
    defaults are used.
    """
    # clear env vars that might interfere
    # We clear the specific env vars we care about, or all of them depending on safety.
    # Since we can't easily clear ALL with monkeypatch without iterating, 
    # we'll just mock os.environ.get to return None or specific strategy if needed.
    # But monkeypatch.setattr(os, 'environ', {}) might be too aggressive for system stuff.
    # Better to just allow the test to run if the env is clean, or explicitly unset the ones we know.
    
    # However, doorman config checks specific keys. Let's unset those.
    keys_to_unset = [
        "DOORMAN_API_KEY", "DOORMAN_API_URL", "REDIS_URL", 
        "CELERY_BROKER_URL", "DOORMAN_LOCAL_MODE", "CHECK_INTERVAL"
    ]
    for key in keys_to_unset:
        monkeypatch.delenv(key, raising=False)

    config = load_config(config_path=None)
    
    assert isinstance(config, Config)
    assert config.check_interval_seconds == 30
    assert config.api_url == "https://api.doorman.com"
    assert config.local_mode is False
    assert config.monitored_queues == ["celery", "default", "priority", "emails"]
