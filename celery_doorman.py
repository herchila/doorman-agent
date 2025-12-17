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
from dataclasses import dataclass, field, asdict
from enum import Enum
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
    # Connections
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_app_name: str = "tasks"
    
    # Webhooks
    slack_webhook_url: Optional[str] = None
    pagerduty_routing_key: Optional[str] = None
    generic_webhook_url: Optional[str] = None
    
    # Behavior
    check_interval_seconds: int = 30
    alert_cooldown_seconds: int = 300  # Don't repeat same alert for 5 min
    auto_kill_zombie_tasks: bool = False
    
    # Thresholds
    thresholds: AlertThresholds = field(default_factory=AlertThresholds)
    
    # Queues to monitor (empty = all)
    monitored_queues: List[str] = field(default_factory=lambda: ['celery', 'default', 'priority', 'emails', 'payments'])


# =============================================================================
# DATA MODELS
# =============================================================================

class AlertSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


@dataclass
class Alert:
    """Represents an alert to be sent"""
    severity: AlertSeverity
    title: str
    message: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metrics: Dict[str, Any] = field(default_factory=dict)
    alert_type: str = ""
    
    def to_dict(self) -> Dict:
        return {
            "severity": self.severity.value,
            "title": self.title,
            "message": self.message,
            "timestamp": self.timestamp,
            "metrics": self.metrics,
            "alert_type": self.alert_type
        }


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
# RISK ANALYZER
# =============================================================================

class RiskAnalyzer:
    """Analyzes metrics and generates alerts based on configured thresholds"""
    
    def __init__(self, config: Config, logger: StructuredLogger):
        self.config = config
        self.logger = logger
        self.thresholds = config.thresholds
        self.alert_history: Dict[str, float] = {}  # alert_key -> last_sent_timestamp
    
    def _should_alert(self, alert_key: str) -> bool:
        """Checks if we should send alert (cooldown)"""
        now = time.time()
        last_sent = self.alert_history.get(alert_key, 0)
        
        if now - last_sent > self.config.alert_cooldown_seconds:
            self.alert_history[alert_key] = now
            return True
        return False
    
    def analyze(self, metrics: SystemMetrics) -> List[Alert]:
        """Analyzes metrics and returns list of alerts"""
        alerts = []
        
        # =================================================================
        # Scenario A: Massive Backlog (Backlog Explosion)
        # =================================================================
        if metrics.total_pending_tasks > self.thresholds.max_queue_size:
            alert_key = "backlog_explosion"
            if self._should_alert(alert_key):
                alerts.append(Alert(
                    severity=AlertSeverity.CRITICAL,
                    title="🚨 Backlog Explosion Detected",
                    message=f"Total pending tasks ({metrics.total_pending_tasks}) exceeds threshold ({self.thresholds.max_queue_size})",
                    alert_type=alert_key,
                    metrics={
                        "total_pending": metrics.total_pending_tasks,
                        "threshold": self.thresholds.max_queue_size,
                        "queues": {q.name: q.depth for q in metrics.queues}
                    }
                ))
        
        # =================================================================
        # Scenario B: Silent Death (Workers down but queue filling)
        # =================================================================
        if metrics.alive_workers == 0 and metrics.total_pending_tasks > 0:
            alert_key = "workers_down"
            if self._should_alert(alert_key):
                alerts.append(Alert(
                    severity=AlertSeverity.EMERGENCY,
                    title="🔴 EMERGENCY: All Workers Down",
                    message=f"No workers responding but {metrics.total_pending_tasks} tasks are pending!",
                    alert_type=alert_key,
                    metrics={
                        "pending_tasks": metrics.total_pending_tasks,
                        "total_workers": metrics.total_workers,
                        "alive_workers": 0
                    }
                ))
        
        # Some workers down (partial)
        elif metrics.total_workers > 0 and metrics.alive_workers < metrics.total_workers:
            dead_count = metrics.total_workers - metrics.alive_workers
            alert_key = f"workers_partial_down_{dead_count}"
            if self._should_alert(alert_key):
                alerts.append(Alert(
                    severity=AlertSeverity.WARNING,
                    title="⚠️ Workers Partially Down",
                    message=f"{dead_count} of {metrics.total_workers} workers not responding",
                    alert_type="workers_partial_down",
                    metrics={
                        "total_workers": metrics.total_workers,
                        "alive_workers": metrics.alive_workers,
                        "dead_workers": dead_count
                    }
                ))
        
        # =================================================================
        # Scenario C: Zombie Tasks (Stuck tasks)
        # =================================================================
        for stuck_task in metrics.stuck_tasks:
            alert_key = f"stuck_task_{stuck_task['task_id']}"
            if self._should_alert(alert_key):
                alerts.append(Alert(
                    severity=AlertSeverity.WARNING,
                    title="🧟 Zombie Task Detected",
                    message=f"Task {stuck_task['task_name']} running for {stuck_task['runtime_seconds']/60:.1f} minutes",
                    alert_type="stuck_task",
                    metrics=stuck_task
                ))
        
        # =================================================================
        # Scenario D: High Latency in Critical Queues
        # =================================================================
        for queue in metrics.queues:
            if queue.oldest_task_age_seconds is not None:
                if queue.oldest_task_age_seconds > self.thresholds.max_wait_time_seconds:
                    # Higher severity for critical queues
                    is_critical = queue.name in self.thresholds.critical_queues
                    severity = AlertSeverity.CRITICAL if is_critical else AlertSeverity.WARNING
                    
                    alert_key = f"high_latency_{queue.name}"
                    if self._should_alert(alert_key):
                        alerts.append(Alert(
                            severity=severity,
                            title=f"⏰ High Latency in Queue: {queue.name}",
                            message=f"Oldest task waiting {queue.oldest_task_age_seconds:.0f}s (threshold: {self.thresholds.max_wait_time_seconds}s)",
                            alert_type="high_latency",
                            metrics={
                                "queue": queue.name,
                                "wait_time_seconds": queue.oldest_task_age_seconds,
                                "threshold_seconds": self.thresholds.max_wait_time_seconds,
                                "queue_depth": queue.depth,
                                "is_critical_queue": is_critical
                            }
                        ))
        
        # =================================================================
        # Scenario E: Critical Queue with Backlog
        # =================================================================
        for queue in metrics.queues:
            if queue.name in self.thresholds.critical_queues and queue.depth > 100:
                alert_key = f"critical_queue_backlog_{queue.name}"
                if self._should_alert(alert_key):
                    alerts.append(Alert(
                        severity=AlertSeverity.WARNING,
                        title=f"📊 Critical Queue Backlog: {queue.name}",
                        message=f"Critical queue '{queue.name}' has {queue.depth} pending tasks",
                        alert_type="critical_queue_backlog",
                        metrics={
                            "queue": queue.name,
                            "depth": queue.depth
                        }
                    ))
        
        # =================================================================
        # Scenario F: Connection lost
        # =================================================================
        if not metrics.redis_connected:
            alert_key = "redis_disconnected"
            if self._should_alert(alert_key):
                alerts.append(Alert(
                    severity=AlertSeverity.EMERGENCY,
                    title="🔴 Redis Connection Lost",
                    message="Cannot connect to Redis broker",
                    alert_type=alert_key,
                    metrics={"redis_connected": False}
                ))
        
        if not metrics.celery_connected and metrics.redis_connected:
            alert_key = "celery_disconnected"
            if self._should_alert(alert_key):
                alerts.append(Alert(
                    severity=AlertSeverity.WARNING,
                    title="⚠️ Celery Inspect Failed",
                    message="Cannot inspect Celery workers (they may be down or unreachable)",
                    alert_type=alert_key,
                    metrics={"celery_connected": False}
                ))
        
        return alerts


# =============================================================================
# NOTIFIER
# =============================================================================

class Notifier:
    """Sends alerts to different destinations"""
    
    def __init__(self, config: Config, logger: StructuredLogger):
        self.config = config
        self.logger = logger
    
    def _http_post(self, url: str, payload: Dict, headers: Dict = None) -> bool:
        """Sends POST request"""
        if not headers:
            headers = {}
        headers['Content-Type'] = 'application/json'
        
        try:
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(url, data=data, headers=headers, method='POST')
            
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status == 200
                
        except urllib.error.URLError as e:
            self.logger.error("HTTP request failed", url=url, error=str(e))
            return False
        except Exception as e:
            self.logger.error("Notification failed", url=url, error=str(e))
            return False
    
    def send_to_slack(self, alert: Alert) -> bool:
        """Sends alert to Slack webhook"""
        if not self.config.slack_webhook_url:
            return False
        
        # Map severity to color
        color_map = {
            AlertSeverity.INFO: "#36a64f",       # Green
            AlertSeverity.WARNING: "#ffcc00",    # Yellow
            AlertSeverity.CRITICAL: "#ff6600",   # Orange
            AlertSeverity.EMERGENCY: "#ff0000"   # Red
        }
        
        payload = {
            "attachments": [{
                "color": color_map.get(alert.severity, "#808080"),
                "title": alert.title,
                "text": alert.message,
                "fields": [
                    {"title": k, "value": str(v), "short": True}
                    for k, v in alert.metrics.items()
                ][:10],  # Limit fields
                "footer": "Celery Doorman",
                "ts": int(time.time())
            }]
        }
        
        success = self._http_post(self.config.slack_webhook_url, payload)
        if success:
            self.logger.info("Slack notification sent", alert_type=alert.alert_type)
        return success
    
    def send_to_pagerduty(self, alert: Alert) -> bool:
        """Sends alert to PagerDuty"""
        if not self.config.pagerduty_routing_key:
            return False
        
        # Map severity to PagerDuty severity
        severity_map = {
            AlertSeverity.INFO: "info",
            AlertSeverity.WARNING: "warning",
            AlertSeverity.CRITICAL: "critical",
            AlertSeverity.EMERGENCY: "critical"
        }
        
        payload = {
            "routing_key": self.config.pagerduty_routing_key,
            "event_action": "trigger",
            "payload": {
                "summary": f"{alert.title}: {alert.message}",
                "severity": severity_map.get(alert.severity, "warning"),
                "source": "celery-doorman",
                "custom_details": alert.metrics
            }
        }
        
        success = self._http_post(
            "https://events.pagerduty.com/v2/enqueue",
            payload
        )
        if success:
            self.logger.info("PagerDuty notification sent", alert_type=alert.alert_type)
        return success
    
    def send_to_generic_webhook(self, alert: Alert) -> bool:
        """Sends to generic webhook (full JSON)"""
        if not self.config.generic_webhook_url:
            return False
        
        payload = alert.to_dict()
        payload['source'] = 'celery-doorman'
        
        success = self._http_post(self.config.generic_webhook_url, payload)
        if success:
            self.logger.info("Webhook notification sent", alert_type=alert.alert_type)
        return success
    
    def notify(self, alert: Alert):
        """Sends alert to all configured destinations"""
        sent = False
        
        if self.config.slack_webhook_url:
            sent = self.send_to_slack(alert) or sent
            
        if self.config.pagerduty_routing_key:
            sent = self.send_to_pagerduty(alert) or sent
            
        if self.config.generic_webhook_url:
            sent = self.send_to_generic_webhook(alert) or sent
        
        if not sent:
            # At least log the alert
            self.logger.warning(
                "Alert generated but no notification channel configured",
                alert=alert.to_dict()
            )


# =============================================================================
# MAIN DOORMAN
# =============================================================================

class CeleryDoorman:
    """The silent guardian of your queues"""
    
    def __init__(self, config: Config):
        self.config = config
        self.logger = StructuredLogger("celery-doorman")
        self.collector = MetricsCollector(config, self.logger)
        self.analyzer = RiskAnalyzer(config, self.logger)
        self.notifier = Notifier(config, self.logger)
        self.running = False
        
    def setup_signal_handlers(self):
        """Configures handlers for graceful shutdown"""
        def handle_shutdown(signum, frame):
            self.logger.info("Shutdown signal received", signal=signum)
            self.running = False
            
        signal.signal(signal.SIGTERM, handle_shutdown)
        signal.signal(signal.SIGINT, handle_shutdown)
    
    def check_once(self) -> SystemMetrics:
        """Executes one monitoring cycle"""
        # Collect metrics
        metrics = self.collector.collect()
        
        # Log status (always, for observability)
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
        
        # Analyze and generate alerts
        alerts = self.analyzer.analyze(metrics)
        
        # Send notifications
        for alert in alerts:
            self.logger.warning(
                "Alert triggered",
                severity=alert.severity.value,
                alert_type=alert.alert_type,
                title=alert.title
            )
            self.notifier.notify(alert)
        
        return metrics
    
    def run(self):
        """Runs the main daemon loop"""
        self.logger.info(
            "Celery Doorman starting",
            check_interval=self.config.check_interval_seconds,
            monitored_queues=self.config.monitored_queues
        )
        
        # Connect
        if not self.collector.connect():
            self.logger.error("Failed to establish connections, exiting")
            sys.exit(1)
        
        self.setup_signal_handlers()
        self.running = True
        
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
        
        self.logger.info("Celery Doorman stopped")


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
                # Map values
                config.redis_url = yaml_config.get('redis_url', config.redis_url)
                config.celery_broker_url = yaml_config.get('celery_broker_url', config.celery_broker_url)
                config.celery_app_name = yaml_config.get('celery_app_name', config.celery_app_name)
                config.slack_webhook_url = yaml_config.get('slack_webhook_url')
                config.pagerduty_routing_key = yaml_config.get('pagerduty_routing_key')
                config.generic_webhook_url = yaml_config.get('generic_webhook_url')
                config.check_interval_seconds = yaml_config.get('check_interval_seconds', config.check_interval_seconds)
                config.alert_cooldown_seconds = yaml_config.get('alert_cooldown_seconds', config.alert_cooldown_seconds)
                config.auto_kill_zombie_tasks = yaml_config.get('auto_kill_zombie_tasks', config.auto_kill_zombie_tasks)
                config.monitored_queues = yaml_config.get('monitored_queues', config.monitored_queues)
                
                # Thresholds
                if 'thresholds' in yaml_config:
                    t = yaml_config['thresholds']
                    config.thresholds.max_queue_size = t.get('max_queue_size', config.thresholds.max_queue_size)
                    config.thresholds.max_wait_time_seconds = t.get('max_wait_time_seconds', config.thresholds.max_wait_time_seconds)
                    config.thresholds.max_task_runtime_seconds = t.get('max_task_runtime_seconds', config.thresholds.max_task_runtime_seconds)
                    config.thresholds.critical_queues = t.get('critical_queues', config.thresholds.critical_queues)
    
    # Environment variables override file
    config.redis_url = os.environ.get('REDIS_URL', config.redis_url)
    config.celery_broker_url = os.environ.get('CELERY_BROKER_URL', config.celery_broker_url)
    config.slack_webhook_url = os.environ.get('SLACK_WEBHOOK_URL', config.slack_webhook_url)
    config.pagerduty_routing_key = os.environ.get('PAGERDUTY_ROUTING_KEY', config.pagerduty_routing_key)
    config.generic_webhook_url = os.environ.get('GENERIC_WEBHOOK_URL', config.generic_webhook_url)
    
    if os.environ.get('CHECK_INTERVAL'):
        config.check_interval_seconds = int(os.environ['CHECK_INTERVAL'])
    if os.environ.get('MAX_QUEUE_SIZE'):
        config.thresholds.max_queue_size = int(os.environ['MAX_QUEUE_SIZE'])
    if os.environ.get('MAX_WAIT_TIME'):
        config.thresholds.max_wait_time_seconds = int(os.environ['MAX_WAIT_TIME'])
    
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
        description='Celery Doorman - Silent Queue Guardian',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run as daemon (production)
  python celery_doorman.py --config config.yaml
  
  # Single execution (for cron)
  python celery_doorman.py --once
  
  # With environment variables
  REDIS_URL=redis://localhost:6379/0 SLACK_WEBHOOK_URL=https://... python celery_doorman.py

Simulation Mode (for demos/testing):
  # Healthy scenario: 1 worker, no pending tasks
  python celery_doorman.py --simulate --workers 1
  
  # Workers down scenario (WARNING)
  python celery_doorman.py --simulate --workers 0
  
  # EMERGENCY scenario: workers down + tasks pending
  python celery_doorman.py --simulate --workers 0 --enqueue 5
  
  # Backlog scenario: tasks accumulating
  python celery_doorman.py --simulate --workers 1 --enqueue 50

Supported environment variables:
  REDIS_URL              - Redis connection URL
  CELERY_BROKER_URL      - Celery broker URL
  SLACK_WEBHOOK_URL      - Slack webhook for alerts
  PAGERDUTY_ROUTING_KEY  - PagerDuty routing key
  GENERIC_WEBHOOK_URL    - Generic webhook
  CHECK_INTERVAL         - Check interval in seconds
  MAX_QUEUE_SIZE         - Maximum queue size threshold
  MAX_WAIT_TIME          - Maximum wait time threshold in seconds
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
        help='Run only once (for cron jobs)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Do not send notifications, only log'
    )
    
    # Simulation arguments
    parser.add_argument(
        '--simulate', '-s',
        action='store_true',
        help='Run in simulation mode (starts local Redis + workers)'
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
    
    # Simulation mode
    if args.simulate:
        run_simulation(args.workers, args.enqueue)
        return
    
    # Production mode
    # Load configuration
    config = load_config(args.config)
    
    # Dry run: disable webhooks
    if args.dry_run:
        config.slack_webhook_url = None
        config.pagerduty_routing_key = None
        config.generic_webhook_url = None
    
    # Create and run Doorman
    doorman = CeleryDoorman(config)
    
    if not doorman.collector.connect():
        print("❌ Could not connect to Redis/Celery")
        sys.exit(1)
    
    if args.once:
        # Cron mode: single execution
        doorman.check_once()
    else:
        # Daemon mode: infinite loop
        doorman.run()


if __name__ == '__main__':
    main()
