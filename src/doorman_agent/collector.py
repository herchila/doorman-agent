import json
import time

from typing import Dict
from datetime import datetime, timezone
from typing import Optional

from src.doorman_agent.celery_doorman import CELERY_AVAILABLE, REDIS_AVAILABLE
from src.doorman_agent.config import Config
from src.doorman_agent.logger import StructuredLogger
from src.doorman_agent.models import QueueMetrics, SystemMetrics, WorkerMetrics


class MetricsCollector:
    """Collects metrics from Redis and Celery"""
    
    def __init__(self, config: Config, logger: StructuredLogger):
        self.config = config
        self.logger = logger
        self.redis_client: Optional[redis.Redis] = None
        self.celery_app: Optional[Celery] = None
        
    def connect(self) -> bool:
        """Establishes connections with Redis and Celery"""
        success = True
        
        # Connect to Redis
        if REDIS_AVAILABLE:
            try:
                self.redis_client = redis.from_url(
                    self.config.redis_url,
                    decode_responses=True,
                    socket_timeout=30,
                    socket_connect_timeout=30
                )
                self.redis_client.ping()
                self.logger.info("Redis connection established", url=self.config.redis_url)
            except Exception as e:
                self.logger.error("Redis connection failed", error=str(e))
                success = False
        else:
            self.logger.warning("Redis library not available")
            success = False
            
        # Connect to Celery
        if CELERY_AVAILABLE:
            try:
                self.celery_app = Celery(
                    self.config.celery_app_name,
                    broker=self.config.celery_broker_url
                )
                self.celery_app.conf.update(
                    broker_connection_timeout=5,
                    broker_connection_retry=False
                )
                self.logger.info("Celery app initialized", broker=self.config.celery_broker_url)
            except Exception as e:
                self.logger.error("Celery initialization failed", error=str(e))
                success = False
        else:
            self.logger.warning("Celery library not available")
            success = False
            
        return success
    
    def get_queue_depth(self, queue_name: str) -> int:
        """Gets the depth of a queue from Redis"""
        if not self.redis_client:
            return 0
            
        try:
            # Celery uses lists in Redis with the queue name
            depth = self.redis_client.llen(queue_name)
            return depth or 0
        except Exception as e:
            self.logger.error("Failed to get queue depth", queue=queue_name, error=str(e))
            return 0
    
    def get_oldest_task_age(self, queue_name: str) -> Optional[float]:
        """
        Estimates the age of the oldest task in the queue.
        Note: This requires inspecting the Celery message, which includes timestamp.
        """
        if not self.redis_client:
            return None
            
        try:
            # Get the oldest message (last in the list, since it's FIFO)
            oldest_message = self.redis_client.lindex(queue_name, -1)
            if not oldest_message:
                return None
                
            # Parse the Celery message (it's JSON)
            try:
                if isinstance(oldest_message, str):
                    task_data = json.loads(oldest_message)
                else:
                    task_data = json.loads(oldest_message.decode('utf-8'))
                    
                # Celery includes 'eta' or we can use the timestamp from body
                # Format varies depending on Celery version
                headers = task_data.get('headers', {})
                
                # Try to get timestamp from different places
                timestamp = None
                
                # Option 1: headers.timestamp (Celery 5.x)
                if 'timestamp' in headers:
                    timestamp = headers['timestamp']
                    
                # Option 2: properties.timestamp
                elif 'properties' in task_data and 'timestamp' in task_data['properties']:
                    timestamp = task_data['properties']['timestamp']
                
                if timestamp:
                    task_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
                    age = (datetime.now(timezone.utc) - task_time).total_seconds()
                    return max(0, age)
                    
            except (json.JSONDecodeError, KeyError, TypeError):
                # If we can't parse, we can't estimate the age
                pass
                
            return None
            
        except Exception as e:
            self.logger.error("Failed to get oldest task age", queue=queue_name, error=str(e))
            return None
    
    def get_worker_stats(self) -> tuple[Dict, Dict, Dict]:
        """Gets worker statistics via Celery inspect"""
        active = {}
        reserved = {}
        stats = {}
        
        if not self.celery_app:
            return active, reserved, stats
            
        try:
            inspector = self.celery_app.control.inspect(timeout=5)
            
            # Active tasks (running now)
            active = inspector.active() or {}
            
            # Reserved tasks (in worker memory, pending execution)
            reserved = inspector.reserved() or {}
            
            # General worker statistics
            stats = inspector.stats() or {}
            
        except Exception as e:
            self.logger.error("Failed to inspect Celery workers", error=str(e))
            
        return active, reserved, stats
    
    def collect(self) -> SystemMetrics:
        """Collects all system metrics"""
        metrics = SystemMetrics(
            timestamp=datetime.now(timezone.utc).isoformat()
        )
        
        # Verify Redis connection
        if self.redis_client:
            try:
                self.redis_client.ping()
                metrics.redis_connected = True
            except:
                metrics.redis_connected = False
        
        # Collect queue metrics
        for queue_name in self.config.monitored_queues:
            depth = self.get_queue_depth(queue_name)
            oldest_age = self.get_oldest_task_age(queue_name)
            
            queue_metrics = QueueMetrics(
                name=queue_name,
                depth=depth,
                oldest_task_age_seconds=oldest_age
            )
            metrics.queues.append(queue_metrics)
            metrics.total_pending_tasks += depth
        
        # Collect worker metrics
        active, reserved, stats = self.get_worker_stats()
        
        if active or reserved or stats:
            metrics.celery_connected = True
            
        # Process workers
        all_workers = set(active.keys()) | set(reserved.keys()) | set(stats.keys())
        metrics.total_workers = len(all_workers)
        
        for worker_name in all_workers:
            worker_active = active.get(worker_name, [])
            worker_stats = stats.get(worker_name, {})
            
            worker_metrics = WorkerMetrics(
                name=worker_name,
                active_tasks=len(worker_active),
                is_alive=worker_name in stats
            )
            metrics.workers.append(worker_metrics)
            
            if worker_metrics.is_alive:
                metrics.alive_workers += 1
            
            metrics.total_active_tasks += len(worker_active)
            
            # Detect stuck tasks (zombies)
            for task in worker_active:
                if isinstance(task, dict):
                    time_start = task.get('time_start')
                    if time_start:
                        runtime = time.time() - time_start
                        if runtime > self.config.thresholds.max_task_runtime_seconds:
                            metrics.stuck_tasks.append({
                                'task_id': task.get('id', 'unknown'),
                                'task_name': task.get('name', 'unknown'),
                                'worker': worker_name,
                                'runtime_seconds': runtime,
                                'started_at': datetime.fromtimestamp(time_start, tz=timezone.utc).isoformat()
                            })
        
        return metrics
