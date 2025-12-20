from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any


@dataclass
class QueueMetrics:
    """Metrics for a specific queue"""
    name: str
    depth: int = 0
    oldest_task_age_seconds: Optional[float] = None
    
    
@dataclass
class WorkerMetrics:
    """Metrics for a worker"""
    name: str
    active_tasks: int = 0
    last_heartbeat: Optional[str] = None
    is_alive: bool = True


@dataclass
class SystemMetrics:
    """Complete system metrics"""
    timestamp: str
    total_pending_tasks: int = 0
    total_active_tasks: int = 0
    total_workers: int = 0
    alive_workers: int = 0
    queues: List[QueueMetrics] = field(default_factory=list)
    workers: List[WorkerMetrics] = field(default_factory=list)
    stuck_tasks: List[Dict] = field(default_factory=list)
    redis_connected: bool = False
    celery_connected: bool = False
