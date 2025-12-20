"""
Data models for Doorman Agent
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class AlertThresholds(BaseModel):
    """Configurable thresholds for alerts"""
    max_queue_size: int = 1000
    max_wait_time_seconds: int = 60
    max_task_runtime_seconds: int = 1800  # 30 minutes
    worker_heartbeat_timeout_seconds: int = 120
    critical_queues: List[str] = Field(default_factory=lambda: ['payments', 'emails', 'celery'])


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
    
    # Queues to monitor (empty = all)
    monitored_queues: List[str] = Field(default_factory=lambda: ['celery', 'default', 'priority', 'emails', 'payments'])


class QueueMetrics(BaseModel):
    """Metrics for a specific queue"""
    name: str
    depth: int = 0
    oldest_task_age_seconds: Optional[float] = None


class WorkerMetrics(BaseModel):
    """Metrics for a worker"""
    name: str
    active_tasks: int = 0
    last_heartbeat: Optional[str] = None
    is_alive: bool = True


class SystemMetrics(BaseModel):
    """Complete system metrics"""
    timestamp: str
    total_pending_tasks: int = 0
    total_active_tasks: int = 0
    total_workers: int = 0
    alive_workers: int = 0
    queues: List[QueueMetrics] = Field(default_factory=list)
    workers: List[WorkerMetrics] = Field(default_factory=list)
    stuck_tasks: List[Dict[str, Any]] = Field(default_factory=list)
    redis_connected: bool = False
    celery_connected: bool = False
