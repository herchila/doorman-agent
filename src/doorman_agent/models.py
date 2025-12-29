"""
Data models for Doorman Agent
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class AlertThresholds(BaseModel):
    """Configurable thresholds for alerts"""

    max_queue_size: int = 1000
    max_wait_time_seconds: int = 60
    max_task_runtime_seconds: int = 1800  # 30 minutes
    worker_heartbeat_timeout_seconds: int = 120
    critical_queues: list[str] = Field(default_factory=list)  # empty = none critical


class PrivacyConfig(BaseModel):
    """Privacy-related configuration"""

    # If True, task signatures are sanitized (e.g., "app.tasks.process_user_123" -> "app.tasks.process_user_[id]")
    # If False, task signatures are sent as-is (use only if you're sure they don't contain PII)
    sanitize_task_signatures: bool = True


class Config(BaseModel):
    """Complete Doorman configuration"""

    # API Connection (required for production)
    api_key: Optional[str] = None
    api_url: str = "https://api.doorman.com"

    # Local mode: skip API, only log metrics
    local_mode: bool = False

    # Local connections (for metrics collection)
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_app_name: str = "tasks"

    # Behavior
    check_interval_seconds: int = 30

    # Thresholds (used locally for logging, API does the actual alerting)
    thresholds: AlertThresholds = Field(default_factory=AlertThresholds)

    # Privacy settings
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)

    # Queues to monitor (empty = auto-discover from Celery workers)
    monitored_queues: list[str] = Field(default_factory=list)


class QueueMetrics(BaseModel):
    """Metrics for a specific queue"""

    name: str
    depth: int = 0
    oldest_task_age_seconds: Optional[float] = None


class WorkerMetrics(BaseModel):
    """Metrics for a worker"""

    name: str
    active_tasks: int = 0
    concurrency: int = 0  # max-concurrency from pool
    last_heartbeat: Optional[str] = None
    is_alive: bool = True


class SystemMetrics(BaseModel):
    """Complete system metrics"""

    timestamp: str
    total_pending_tasks: int = 0
    total_active_tasks: int = 0
    total_workers: int = 0
    alive_workers: int = 0
    total_concurrency: int = 0  # sum of all workers' max-concurrency
    saturation_pct: float = 0.0  # (active_tasks / total_concurrency) * 100
    max_latency_sec: Optional[float] = None  # oldest task age across all queues
    queues: list[QueueMetrics] = Field(default_factory=list)
    workers: list[WorkerMetrics] = Field(default_factory=list)
    stuck_tasks: list[dict[str, Any]] = Field(default_factory=list)
    redis_connected: bool = False
    celery_connected: bool = False
