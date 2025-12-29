"""
Configuration loading for Doorman Agent
"""

from __future__ import annotations

import os
from typing import Optional

from doorman_agent.models import AlertThresholds, Config, PrivacyConfig

AGENT_VERSION = "0.1.0-alpha.2"

# Optional YAML support
try:
    import yaml

    YAML_AVAILABLE = True
except ImportError:
    yaml = None
    YAML_AVAILABLE = False


def load_config(config_path: Optional[str] = None) -> Config:
    """Loads configuration from YAML file or environment variables"""
    config_data: dict = {}

    # Load from YAML file if it exists
    if config_path and os.path.exists(config_path):
        if not YAML_AVAILABLE:
            print("⚠️  PyYAML not installed. Install with: pip install pyyaml")
        else:
            with open(config_path) as f:
                yaml_config = yaml.safe_load(f) or {}

            # Map YAML to config dict
            config_data = {
                "api_key": yaml_config.get("api_key"),
                "api_url": yaml_config.get("api_url", "https://api.doorman.com"),
                "local_mode": yaml_config.get("local_mode", False),
                "redis_url": yaml_config.get("redis_url", "redis://localhost:6379/0"),
                "celery_broker_url": yaml_config.get(
                    "celery_broker_url", "redis://localhost:6379/0"
                ),
                "celery_app_name": yaml_config.get("celery_app_name", "tasks"),
                "check_interval_seconds": yaml_config.get("check_interval_seconds", 30),
            }

            # Only set monitored_queues if explicitly configured
            if "monitored_queues" in yaml_config:
                config_data["monitored_queues"] = yaml_config["monitored_queues"]

            # Handle thresholds
            if "thresholds" in yaml_config:
                t = yaml_config["thresholds"]
                threshold_data = {
                    "max_queue_size": t.get("max_queue_size", 1000),
                    "max_wait_time_seconds": t.get("max_wait_time_seconds", 60),
                    "max_task_runtime_seconds": t.get("max_task_runtime_seconds", 1800),
                }
                # Only set critical_queues if explicitly configured
                if "critical_queues" in t:
                    threshold_data["critical_queues"] = t["critical_queues"]
                config_data["thresholds"] = AlertThresholds(**threshold_data)

            # Handle privacy settings
            if "privacy" in yaml_config:
                p = yaml_config["privacy"]
                privacy_data = {}
                if "sanitize_task_signatures" in p:
                    privacy_data["sanitize_task_signatures"] = p["sanitize_task_signatures"]
                config_data["privacy"] = PrivacyConfig(**privacy_data)

    # Environment variables override file
    if os.environ.get("DOORMAN_API_KEY"):
        config_data["api_key"] = os.environ["DOORMAN_API_KEY"]
    if os.environ.get("DOORMAN_API_URL"):
        config_data["api_url"] = os.environ["DOORMAN_API_URL"]
    if os.environ.get("REDIS_URL"):
        config_data["redis_url"] = os.environ["REDIS_URL"]
    if os.environ.get("CELERY_BROKER_URL"):
        config_data["celery_broker_url"] = os.environ["CELERY_BROKER_URL"]

    # Local mode from env
    local_mode_env = os.environ.get("DOORMAN_LOCAL_MODE", "").lower()
    if local_mode_env in ("true", "1", "yes"):
        config_data["local_mode"] = True

    if os.environ.get("CHECK_INTERVAL"):
        config_data["check_interval_seconds"] = int(os.environ["CHECK_INTERVAL"])

    # Privacy settings from env
    sanitize_env = os.environ.get("DOORMAN_SANITIZE_TASK_SIGNATURES", "").lower()
    if sanitize_env in ("false", "0", "no"):
        config_data["privacy"] = PrivacyConfig(sanitize_task_signatures=False)

    # Create and validate config with Pydantic
    return Config(**{k: v for k, v in config_data.items() if v is not None})
