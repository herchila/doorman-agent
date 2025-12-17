#!/usr/bin/env python3
"""
Celery Doorman - Silent Queue Guardian
=======================================
A lightweight process that monitors Redis/Celery status and triggers
early alerts via webhooks when anomalies are detected.

Usage:
    # As daemon
    python celery_doorman.py --config config.yaml
    
    # Single execution (for cron)
    python celery_doorman.py --config config.yaml --once
    
    # With environment variables
    REDIS_URL=redis://localhost:6379/0 SLACK_WEBHOOK=https://... python celery_doorman.py

Author: Celery Doorman
Version: 1.0.0
"""

import os
import sys
import json
import time
import signal
import logging
import argparse
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
import urllib.request
import urllib.error

# Try to import optional dependencies
try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    print("⚠️  redis-py not installed. Install with: pip install redis")

try:
    from celery import Celery
    CELERY_AVAILABLE = True
except ImportError:
    CELERY_AVAILABLE = False
    print("⚠️  celery not installed. Install with: pip install celery")

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class AlertThresholds:
    """Configurable thresholds for alerts"""
    max_queue_size: int = 1000
    max_wait_time_seconds: int = 60
    max_task_runtime_seconds: int = 1800  # 30 minutes
    worker_heartbeat_timeout_seconds: int = 120
    critical_queues: List[str] = field(default_factory=lambda: ['payments', 'emails', 'celery'])


@dataclass
class Config:
    """Complete Doorman configuration"""
    # API Connection (required for production)
    api_key: Optional[str] = None
    api_url: str = "https://api.doorman.com"
    
    # Local connections (for metrics collection)
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_app_name: str = "tasks"
    
    # Behavior
    check_interval_seconds: int = 30
    
    # Thresholds (used locally for logging, API does the actual alerting)
    thresholds: AlertThresholds = field(default_factory=AlertThresholds)
    
    # Queues to monitor (empty = all)
    monitored_queues: List[str] = field(default_factory=lambda: ['celery', 'default', 'priority', 'emails', 'payments'])


# =============================================================================
# DATA MODELS
# =============================================================================

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


# =============================================================================
# STRUCTURED LOGGER
# =============================================================================

class StructuredLogger:
    """Logger that emits structured JSON to stdout"""
    
    def __init__(self, name: str = "celery-doorman"):
        self.name = name
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.INFO)
        
        # Handler for stdout with JSON format
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(self._JsonFormatter())
        self.logger.addHandler(handler)
    
    class _JsonFormatter(logging.Formatter):
        def format(self, record):
            log_obj = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "message": record.getMessage(),
                "logger": record.name
            }
            # Add extra fields if they exist
            if hasattr(record, 'extra_fields'):
                log_obj.update(record.extra_fields)
            return json.dumps(log_obj)
    
    def _log(self, level: int, message: str, **kwargs):
        record = self.logger.makeRecord(
            self.name, level, "", 0, message, (), None
        )
        record.extra_fields = kwargs
        self.logger.handle(record)
    
    def info(self, message: str, **kwargs):
        self._log(logging.INFO, message, **kwargs)
    
    def warning(self, message: str, **kwargs):
        self._log(logging.WARNING, message, **kwargs)
    
    def error(self, message: str, **kwargs):
        self._log(logging.ERROR, message, **kwargs)
    
    def critical(self, message: str, **kwargs):
        self._log(logging.CRITICAL, message, **kwargs)


# =============================================================================
# METRICS COLLECTOR
# =============================================================================

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


# =============================================================================
# API CLIENT
# =============================================================================

# Agent version - update this on releases
AGENT_VERSION = "1.0.0"


class APIClient:
    """
    Client for communicating with doorman.com API.
    The agent only collects and sends metrics - the API handles analysis and notifications.
    """
    
    DEFAULT_API_URL = "https://api.doorman.com"
    
    def __init__(self, api_key: str, api_url: Optional[str] = None, logger: Optional[StructuredLogger] = None):
        self.api_key = api_key
        self.api_url = (api_url or self.DEFAULT_API_URL).rstrip('/')
        self.logger = logger or StructuredLogger("doorman-api-client")
        self._session_id = self._generate_session_id()
        
    def _generate_session_id(self) -> str:
        """Generate a unique session ID for this agent instance"""
        import hashlib
        import platform
        
        unique_string = f"{platform.node()}-{os.getpid()}-{time.time()}"
        return hashlib.sha256(unique_string.encode()).hexdigest()[:16]
    
    def _hash_worker_id(self, worker_name: str) -> str:
        """Generate a privacy-safe hash for worker identification"""
        import hashlib
        return "w-" + hashlib.sha256(worker_name.encode()).hexdigest()[:8]
    
    def _sanitize_display_name(self, worker_name: str) -> str:
        """
        Extract clean display name from worker hostname.
        'celery@worker-1.prod.internal' -> 'worker-1'
        'celery@ip-10-0-1-234' -> 'ip-10-0-1-234'
        """
        # Remove celery@ prefix
        if '@' in worker_name:
            worker_name = worker_name.split('@', 1)[1]
        
        # Remove domain suffix
        if '.' in worker_name:
            worker_name = worker_name.split('.')[0]
        
        return worker_name
    
    def _worker_status(self, is_alive: bool, active_tasks: int, stuck_task_ids: set) -> str:
        """Determine worker status"""
        if not is_alive:
            return "offline"
        # Check if worker has any stuck tasks (will be populated from stuck_tasks list)
        # For now, basic logic - can be extended
        return "online"
    
    def _sanitize_queue_name(self, queue_name: str) -> str:
        """
        Sanitize queue name to remove potential PII.
        Detects email patterns and replaces them.
        """
        import re
        
        # Pattern for email addresses
        email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
        sanitized = re.sub(email_pattern, '[email_redacted]', queue_name)
        
        # Pattern for UUIDs (might contain user IDs)
        uuid_pattern = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
        sanitized = re.sub(uuid_pattern, '[uuid_redacted]', sanitized, flags=re.IGNORECASE)
        
        return sanitized
    
    def _get_headers(self) -> Dict[str, str]:
        """Returns headers for API requests"""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"doorman-agent/{AGENT_VERSION}",
            "X-Agent-Session": self._session_id
        }
    
    def _make_request(self, method: str, endpoint: str, payload: Optional[Dict] = None) -> tuple[bool, Optional[Dict]]:
        """Makes HTTP request to the API"""
        url = f"{self.api_url}{endpoint}"
        
        try:
            data = json.dumps(payload).encode('utf-8') if payload else None
            req = urllib.request.Request(
                url,
                data=data,
                headers=self._get_headers(),
                method=method
            )
            
            with urllib.request.urlopen(req, timeout=30) as response:
                response_data = json.loads(response.read().decode('utf-8'))
                return True, response_data
                
        except urllib.error.HTTPError as e:
            error_body = None
            try:
                error_body = json.loads(e.read().decode('utf-8'))
            except:
                pass
            
            self.logger.error(
                "API request failed",
                endpoint=endpoint,
                status_code=e.code,
                error=error_body or str(e)
            )
            
            # Handle specific error codes
            if e.code == 401:
                self.logger.error("Invalid API key. Please check your DOORMAN_API_KEY.")
            elif e.code == 403:
                self.logger.error("API key does not have permission for this operation.")
            elif e.code == 429:
                self.logger.warning("Rate limited. Will retry on next check interval.")
            
            return False, error_body
            
        except urllib.error.URLError as e:
            self.logger.error("API connection failed", endpoint=endpoint, error=str(e))
            return False, None
            
        except Exception as e:
            self.logger.error("Unexpected API error", endpoint=endpoint, error=str(e))
            return False, None
    
    def validate_api_key(self) -> bool:
        """Validates the API key with the server"""
        success, response = self._make_request("GET", "/api/v1/auth/validate")
        
        if success and response:
            self.logger.info(
                "API key validated",
                organization=response.get("organization", "unknown"),
                plan=response.get("plan", "unknown")
            )
            return True
        
        return False
    
    def send_metrics(self, metrics: SystemMetrics) -> bool:
        """
        Sends collected metrics to the API using privacy-first schema.
        The API will analyze the metrics and trigger notifications if needed.
        """
        # Build worker ID hash lookup for anomalies references
        worker_hash_lookup = {}
        stuck_worker_refs = set()
        
        # First pass: identify workers with stuck tasks
        for stuck in metrics.stuck_tasks:
            worker_name = stuck.get('worker', '')
            if worker_name:
                stuck_worker_refs.add(worker_name)
        
        # Build workers list with privacy-safe identifiers
        workers_payload = []
        for w in metrics.workers:
            id_hash = self._hash_worker_id(w.name)
            worker_hash_lookup[w.name] = id_hash
            
            # Determine status
            if not w.is_alive:
                status = "offline"
            elif w.name in stuck_worker_refs:
                status = "stuck"
            else:
                status = "online"
            
            workers_payload.append({
                "id_hash": id_hash,
                "display_name": self._sanitize_display_name(w.name),
                "active_tasks": w.active_tasks,
                "status": status
            })
        
        # Build anomalies list
        anomalies_payload = []
        for stuck in metrics.stuck_tasks:
            worker_name = stuck.get('worker', '')
            anomalies_payload.append({
                "type": "stuck_task",
                "task_id": stuck.get('task_id', 'unknown'),
                "task_signature": stuck.get('task_name', 'unknown'),
                "worker_ref": worker_hash_lookup.get(worker_name, "w-unknown"),
                "duration_sec": stuck.get('runtime_seconds', 0),
                "started_at": stuck.get('started_at'),
                "detected_at": metrics.timestamp
            })
        
        # Build final payload
        payload = {
            "timestamp": metrics.timestamp,
            "agent_version": AGENT_VERSION,
            "agent_session": self._session_id,
            
            "metrics": {
                "total_pending": metrics.total_pending_tasks,
                "total_active": metrics.total_active_tasks,
                "total_workers": metrics.total_workers,
                "alive_workers": metrics.alive_workers
            },
            
            "infra_health": {
                "redis": metrics.redis_connected,
                "celery": metrics.celery_connected
            },
            
            "queues": [
                {
                    "name": self._sanitize_queue_name(q.name),
                    "depth": q.depth,
                    "latency_sec": q.oldest_task_age_seconds
                }
                for q in metrics.queues
            ],
            
            "workers": workers_payload,
            
            "anomalies": anomalies_payload
        }
        
        import ipdb; ipdb.set_trace()
        success, response = self._make_request("POST", "/api/v1/metrics", payload)
        
        if success:
            self.logger.info(
                "Metrics sent to API",
                total_pending=metrics.total_pending_tasks,
                alive_workers=metrics.alive_workers,
                anomalies_count=len(anomalies_payload),
                alerts_triggered=response.get("alerts_triggered", 0) if response else 0
            )
        
        return success
    
    def send_heartbeat(self) -> bool:
        """Sends a heartbeat to indicate the agent is alive"""
        payload = {
            "agent_session": self._session_id,
            "agent_version": AGENT_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        success, _ = self._make_request("POST", "/api/v1/heartbeat", payload)
        return success


# =============================================================================
# MAIN DOORMAN AGENT
# =============================================================================

class CeleryDoorman:
    """
    The Doorman Agent - a lightweight metrics collector that sends data to doorman.com.
    
    The agent is intentionally "dumb" - it only collects metrics and sends them to the API.
    All analysis, alerting logic, and notifications are handled server-side.
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.logger = StructuredLogger("doorman-agent")
        self.collector = MetricsCollector(config, self.logger)
        self.api_client: Optional[APIClient] = None
        self.running = False
        self._consecutive_failures = 0
        self._max_consecutive_failures = 10
        
        # Initialize API client if API key is provided
        if config.api_key:
            self.api_client = APIClient(
                api_key=config.api_key,
                api_url=config.api_url,
                logger=self.logger
            )
        
    def setup_signal_handlers(self):
        """Configures handlers for graceful shutdown"""
        def handle_shutdown(signum, frame):
            self.logger.info("Shutdown signal received", signal=signum)
            self.running = False
            
        signal.signal(signal.SIGTERM, handle_shutdown)
        signal.signal(signal.SIGINT, handle_shutdown)
    
    def check_once(self) -> SystemMetrics:
        """Executes one monitoring cycle: collect metrics and send to API"""
        # Collect metrics
        metrics = self.collector.collect()
        
        # Log status locally (always, for observability)
        self.logger.info(
            "Health check completed",
            pending_tasks=metrics.total_pending_tasks,
            active_tasks=metrics.total_active_tasks,
            alive_workers=metrics.alive_workers,
            total_workers=metrics.total_workers,
            redis_connected=metrics.redis_connected,
            celery_connected=metrics.celery_connected,
            stuck_tasks_count=len(metrics.stuck_tasks)
        )
        
        # Send metrics to API if configured
        if self.api_client:
            success = self.api_client.send_metrics(metrics)
            
            import ipdb; ipdb.set_trace()
            if success:
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1
                
                if self._consecutive_failures >= self._max_consecutive_failures:
                    self.logger.error(
                        "Too many consecutive API failures",
                        count=self._consecutive_failures,
                        action="Agent will continue collecting but metrics are not being sent"
                    )
        else:
            # No API client - just log locally (useful for testing/simulation)
            self.logger.warning("No API key configured - metrics are only logged locally")
        
        return metrics
    
    def run(self):
        """Runs the main agent loop"""
        self.logger.info(
            "Doorman Agent starting",
            check_interval=self.config.check_interval_seconds,
            monitored_queues=self.config.monitored_queues,
            api_url=self.config.api_url if self.api_client else "not configured"
        )
        
        # Validate API key if configured
        if self.api_client:
            self.logger.info("Validating API key...")
            if not self.api_client.validate_api_key():
                self.logger.error("API key validation failed. Please check your DOORMAN_API_KEY.")
                sys.exit(1)
            self.logger.info("API key validated successfully")
        
        # Connect to Redis/Celery
        if not self.collector.connect():
            self.logger.error("Failed to establish connections, exiting")
            sys.exit(1)
        
        self.setup_signal_handlers()
        self.running = True
        
        self.logger.info("Agent is now running. Press Ctrl+C to stop.")
        
        while self.running:
            try:
                self.check_once()
            except Exception as e:
                self.logger.error("Error in check cycle", error=str(e))
            
            # Sleep in small intervals to respond to signals
            for _ in range(self.config.check_interval_seconds):
                if not self.running:
                    break
                time.sleep(1)
        
        self.logger.info("Doorman Agent stopped")


# =============================================================================
# CONFIGURATION FROM FILE/ENV
# =============================================================================

def load_config(config_path: Optional[str] = None) -> Config:
    """Loads configuration from YAML file or environment variables"""
    config = Config()
    
    # Load from YAML file if it exists
    if config_path and os.path.exists(config_path):
        if not YAML_AVAILABLE:
            print("⚠️  PyYAML not installed. Install with: pip install pyyaml")
        else:
            with open(config_path, 'r') as f:
                yaml_config = yaml.safe_load(f)
                
            if yaml_config:
                # API settings
                config.api_key = yaml_config.get('api_key', config.api_key)
                config.api_url = yaml_config.get('api_url', config.api_url)
                
                # Connection settings
                config.redis_url = yaml_config.get('redis_url', config.redis_url)
                config.celery_broker_url = yaml_config.get('celery_broker_url', config.celery_broker_url)
                config.celery_app_name = yaml_config.get('celery_app_name', config.celery_app_name)
                
                # Behavior
                config.check_interval_seconds = yaml_config.get('check_interval_seconds', config.check_interval_seconds)
                config.monitored_queues = yaml_config.get('monitored_queues', config.monitored_queues)
                
                # Thresholds (for local logging)
                if 'thresholds' in yaml_config:
                    t = yaml_config['thresholds']
                    config.thresholds.max_queue_size = t.get('max_queue_size', config.thresholds.max_queue_size)
                    config.thresholds.max_wait_time_seconds = t.get('max_wait_time_seconds', config.thresholds.max_wait_time_seconds)
                    config.thresholds.max_task_runtime_seconds = t.get('max_task_runtime_seconds', config.thresholds.max_task_runtime_seconds)
                    config.thresholds.critical_queues = t.get('critical_queues', config.thresholds.critical_queues)
    
    # Environment variables override file (API key from env is recommended)
    config.api_key = os.environ.get('DOORMAN_API_KEY', config.api_key)
    config.api_url = os.environ.get('DOORMAN_API_URL', config.api_url)
    config.redis_url = os.environ.get('REDIS_URL', config.redis_url)
    config.celery_broker_url = os.environ.get('CELERY_BROKER_URL', config.celery_broker_url)
    
    if os.environ.get('CHECK_INTERVAL'):
        config.check_interval_seconds = int(os.environ['CHECK_INTERVAL'])
    
    return config


# =============================================================================
# SIMULATION MODE
# =============================================================================

class SimulationEnvironment:
    """
    Self-contained simulation environment with Redis and Celery workers.
    Used for demos and testing without external dependencies.
    """
    
    def __init__(self, num_workers: int, logger: StructuredLogger):
        self.num_workers = num_workers
        self.logger = logger
        self.redis_process: Optional[Any] = None
        self.worker_processes: List[Any] = []
        self.redis_port = 6399  # Use non-standard port to avoid conflicts
        self.temp_dir: Optional[str] = None
        
    def _check_redis_server(self) -> bool:
        """Check if redis-server is available"""
        import shutil
        return shutil.which('redis-server') is not None
    
    def _create_celery_app_file(self) -> str:
        """Creates a temporary Celery app file for workers"""
        import tempfile
        
        self.temp_dir = tempfile.mkdtemp(prefix='doorman_sim_')
        app_file = os.path.join(self.temp_dir, 'celery_app.py')
        
        celery_app_code = f'''
import time
from celery import Celery

app = Celery(
    'simulation',
    broker='redis://localhost:{self.redis_port}/0',
    backend='redis://localhost:{self.redis_port}/0'
)

app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    worker_prefetch_multiplier=1,
)

@app.task(name='simulation.demo_task')
def demo_task(duration=5):
    """A simple demo task that sleeps for a given duration"""
    time.sleep(duration)
    return {{"status": "completed", "duration": duration}}

@app.task(name='simulation.quick_task')
def quick_task():
    """A quick task for testing"""
    return {{"status": "done"}}

@app.task(name='simulation.zombie_task')
def zombie_task():
    """A task that runs forever (for zombie simulation)"""
    while True:
        time.sleep(1)
'''
        
        with open(app_file, 'w') as f:
            f.write(celery_app_code)
        
        return app_file
    
    def start_redis(self) -> bool:
        """Starts a local Redis server"""
        import subprocess
        
        if not self._check_redis_server():
            self.logger.error(
                "redis-server not found. Install with: "
                "apt-get install redis-server (Ubuntu) or brew install redis (Mac)"
            )
            return False
        
        try:
            # Start Redis on non-standard port
            self.redis_process = subprocess.Popen(
                ['redis-server', '--port', str(self.redis_port), '--daemonize', 'no', '--loglevel', 'warning'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            # Wait for Redis to be ready
            time.sleep(1)
            
            # Test connection
            if REDIS_AVAILABLE:
                test_client = redis.from_url(
                    f'redis://localhost:{self.redis_port}/0',
                    socket_timeout=5,
                    socket_connect_timeout=5
                )
                test_client.ping()
                self.logger.info("Simulation Redis started", port=self.redis_port)
                return True
            
        except Exception as e:
            self.logger.error("Failed to start Redis", error=str(e))
            return False
        
        return False
    
    def start_workers(self) -> bool:
        """Starts Celery workers"""
        import subprocess
        
        if self.num_workers <= 0:
            self.logger.info("No workers requested (simulating workers down scenario)")
            return True
        
        app_file = self._create_celery_app_file()
        
        try:
            for i in range(self.num_workers):
                worker_name = f'sim_worker_{i+1}'
                
                # Start Celery worker
                process = subprocess.Popen(
                    [
                        sys.executable, '-m', 'celery',
                        '-A', 'celery_app',
                        'worker',
                        '--loglevel=WARNING',
                        f'--hostname={worker_name}@%h',
                        '--concurrency=1',
                        '-Q', 'celery,default,simulation'
                    ],
                    cwd=self.temp_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={**os.environ, 'PYTHONPATH': self.temp_dir}
                )
                
                self.worker_processes.append(process)
                self.logger.info("Worker started", worker=worker_name, pid=process.pid)
            
            # Wait for workers to register
            time.sleep(3)
            return True
            
        except Exception as e:
            self.logger.error("Failed to start workers", error=str(e))
            return False
    
    def enqueue_tasks(self, count: int = 1, task_type: str = 'quick') -> bool:
        """Enqueues tasks for simulation"""
        if not REDIS_AVAILABLE:
            return False
        
        try:
            # Connect directly to Redis and enqueue Celery-formatted tasks
            client = redis.from_url(
                f'redis://localhost:{self.redis_port}/0',
                decode_responses=True
            )
            
            for i in range(count):
                task_id = f'sim-task-{int(time.time())}-{i}'
                
                # Celery task message format
                task_message = json.dumps({
                    'body': json.dumps([[], {}]),
                    'headers': {
                        'lang': 'py',
                        'task': f'simulation.{task_type}_task',
                        'id': task_id,
                        'timestamp': time.time()
                    },
                    'properties': {
                        'correlation_id': task_id,
                        'delivery_tag': task_id
                    },
                    'content-type': 'application/json',
                    'content-encoding': 'utf-8'
                })
                
                client.lpush('celery', task_message)
            
            self.logger.info("Tasks enqueued", count=count, task_type=task_type)
            return True
            
        except Exception as e:
            self.logger.error("Failed to enqueue tasks", error=str(e))
            return False
    
    def get_config(self) -> Config:
        """Returns a Config object for the simulation environment"""
        config = Config()
        config.redis_url = f'redis://localhost:{self.redis_port}/0'
        config.celery_broker_url = f'redis://localhost:{self.redis_port}/0'
        config.check_interval_seconds = 10
        config.alert_cooldown_seconds = 30
        config.monitored_queues = ['celery', 'default', 'simulation']
        config.thresholds.max_queue_size = 10  # Lower thresholds for demo
        config.thresholds.max_wait_time_seconds = 30
        return config
    
    def stop(self):
        """Stops all simulation processes"""
        # Stop workers
        for process in self.worker_processes:
            try:
                process.terminate()
                process.wait(timeout=5)
            except:
                process.kill()
        
        # Stop Redis
        if self.redis_process:
            try:
                self.redis_process.terminate()
                self.redis_process.wait(timeout=5)
            except:
                self.redis_process.kill()
        
        # Cleanup temp directory
        if self.temp_dir and os.path.exists(self.temp_dir):
            import shutil
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        
        self.logger.info("Simulation environment stopped")


def run_simulation(num_workers: int, enqueue_tasks: int = 0):
    """
    Runs Doorman in simulation mode with local Redis and configurable workers.
    
    Scenarios:
        --workers 0                    : Workers down scenario (EMERGENCY if tasks pending)
        --workers 0 --enqueue 5        : Workers down + queue filling (EMERGENCY)
        --workers 1                    : Healthy scenario (1 worker)
        --workers 2                    : Healthy scenario (2 workers)
        --workers 1 --enqueue 100      : Backlog scenario (if > threshold)
    """
    logger = StructuredLogger("celery-doorman-sim")
    
    print("\n" + "="*60)
    print("🧪 CELERY DOORMAN - SIMULATION MODE")
    print("="*60)
    print(f"   Workers requested: {num_workers}")
    print(f"   Tasks to enqueue:  {enqueue_tasks}")
    print("="*60 + "\n")
    
    sim = SimulationEnvironment(num_workers, logger)
    
    try:
        # Start Redis
        print("📦 Starting Redis server...")
        if not sim.start_redis():
            print("❌ Failed to start Redis. Is redis-server installed?")
            return
        print("✅ Redis started on port", sim.redis_port)
        
        # Start workers
        if num_workers > 0:
            print(f"\n👷 Starting {num_workers} Celery worker(s)...")
            if not sim.start_workers():
                print("❌ Failed to start workers")
                sim.stop()
                return
            print(f"✅ {num_workers} worker(s) started")
        else:
            print("\n⚠️  No workers started (simulating failure scenario)")
        
        # Enqueue tasks if requested
        if enqueue_tasks > 0:
            print(f"\n📝 Enqueuing {enqueue_tasks} task(s)...")
            if sim.enqueue_tasks(enqueue_tasks):
                print(f"✅ {enqueue_tasks} task(s) enqueued")
            else:
                print("❌ Failed to enqueue tasks")
        
        # Create and run Doorman
        print("\n" + "-"*60)
        print("🚪 Starting Doorman monitoring...")
        print("-"*60 + "\n")
        
        config = sim.get_config()
        doorman = CeleryDoorman(config)
        
        if not doorman.collector.connect():
            print("❌ Doorman failed to connect")
            sim.stop()
            return
        
        # Run monitoring loop
        doorman.setup_signal_handlers()
        doorman.running = True
        
        check_count = 0
        while doorman.running:
            check_count += 1
            print(f"\n--- Health Check #{check_count} ---")
            
            try:
                metrics = doorman.check_once()
                
                # Print summary
                print(f"   📊 Pending tasks:  {metrics.total_pending_tasks}")
                print(f"   👷 Alive workers:  {metrics.alive_workers}/{metrics.total_workers}")
                print(f"   🔗 Redis:          {'✅' if metrics.redis_connected else '❌'}")
                print(f"   🔗 Celery:         {'✅' if metrics.celery_connected else '❌'}")
                
            except Exception as e:
                logger.error("Check failed", error=str(e))
            
            # Sleep with interrupt check
            for _ in range(config.check_interval_seconds):
                if not doorman.running:
                    break
                time.sleep(1)
        
    except KeyboardInterrupt:
        print("\n\n⏹️  Interrupted by user")
    
    finally:
        print("\n🧹 Cleaning up simulation environment...")
        sim.stop()
        print("✅ Done\n")


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Doorman Agent - Celery/Redis Monitoring Agent',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run agent (production mode - sends metrics to doorman.com)
  DOORMAN_API_KEY=your-api-key doorman-agent --config config.yaml
  
  # Single check (for testing)
  DOORMAN_API_KEY=your-api-key doorman-agent --once
  
  # Run without API (local logging only)
  doorman-agent --config config.yaml

Simulation Mode (for demos/testing - no API key required):
  # Healthy scenario: 1 worker, no pending tasks
  doorman-agent --simulate --workers 1
  
  # Workers down scenario
  doorman-agent --simulate --workers 0
  
  # EMERGENCY scenario: workers down + tasks pending
  doorman-agent --simulate --workers 0 --enqueue 5

Environment Variables:
  DOORMAN_API_KEY      - Your doorman.com API key (required for production)
  DOORMAN_API_URL      - API URL (default: https://api.doorman.com)
  REDIS_URL            - Redis connection URL
  CELERY_BROKER_URL    - Celery broker URL
  CHECK_INTERVAL       - Check interval in seconds

Get your API key at: https://doorman.com/dashboard/api-keys
        """
    )
    
    # Production arguments
    parser.add_argument(
        '--config', '-c',
        help='Path to YAML configuration file'
    )
    parser.add_argument(
        '--once', '-1',
        action='store_true',
        help='Run only once (for testing)'
    )
    parser.add_argument(
        '--api-key', '-k',
        help='Doorman API key (can also use DOORMAN_API_KEY env var)'
    )
    parser.add_argument(
        '--api-url',
        help='Doorman API URL (default: https://api.doorman.com)'
    )
    
    # Simulation arguments
    parser.add_argument(
        '--simulate', '-s',
        action='store_true',
        help='Run in simulation mode (starts local Redis + workers, no API required)'
    )
    parser.add_argument(
        '--workers', '-w',
        type=int,
        default=1,
        help='Number of workers to simulate (0 = no workers, simulates failure)'
    )
    parser.add_argument(
        '--enqueue', '-e',
        type=int,
        default=0,
        help='Number of tasks to enqueue in simulation'
    )
    
    args = parser.parse_args()
    
    # Verify minimum dependencies
    if not REDIS_AVAILABLE or not CELERY_AVAILABLE:
        print("\n❌ Missing dependencies. Install with:")
        print("   pip install redis celery pyyaml")
        sys.exit(1)
    
    # Simulation mode (no API key required)
    if args.simulate:
        run_simulation(args.workers, args.enqueue)
        return
    
    # Production mode
    # Load configuration
    config = load_config(args.config)
    
    # Override with CLI arguments if provided
    if args.api_key:
        config.api_key = args.api_key
    if args.api_url:
        config.api_url = args.api_url
    
    # Warn if no API key (but allow for local testing)
    if not config.api_key:
        print("\n⚠️  No API key configured. Metrics will only be logged locally.")
        print("   Set DOORMAN_API_KEY or use --api-key to enable cloud monitoring.")
        print("   Get your API key at: https://doorman.com/dashboard/api-keys\n")
    
    # Create and run agent
    doorman = CeleryDoorman(config)
    
    if not doorman.collector.connect():
        print("❌ Could not connect to Redis/Celery")
        sys.exit(1)
    
    if args.once:
        # Single check mode
        doorman.check_once()
    else:
        # Daemon mode
        doorman.run()


if __name__ == '__main__':
    main()
