"""
Main Doorman Agent class
"""

import signal
import sys
import time
from typing import Optional

from doorman_agent.models import Config, SystemMetrics
from doorman_agent.collector import MetricsCollector
from doorman_agent.api_client import APIClient, AGENT_VERSION
from doorman_agent.logger import StructuredLogger


class DoormanAgent:
    """
    The Doorman Agent - a lightweight metrics collector.
    
    In API mode: collects metrics and sends to doorman.com API
    In local mode: collects metrics and logs them (no API calls)
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.logger = StructuredLogger("doorman-agent")
        self.collector = MetricsCollector(config, self.logger)
        self.api_client: Optional[APIClient] = None
        self.running = False
        self._consecutive_failures = 0
        self._max_consecutive_failures = 10
        
        # Initialize API client only if not in local mode and API key provided
        if not config.local_mode and config.api_key:
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
    
    def _log_metrics_locally(self, metrics: SystemMetrics):
        """Logs metrics in structured format (for local mode)"""
        if self.api_client:
            payload = self.api_client.build_payload(metrics)
        else:
            # Build basic payload without API client
            payload = {
                "timestamp": metrics.timestamp,
                "agent_version": AGENT_VERSION,
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
                    {"name": q.name, "depth": q.depth, "latency_sec": q.oldest_task_age_seconds}
                    for q in metrics.queues
                ],
                "workers": [
                    {"name": w.name, "active_tasks": w.active_tasks, "is_alive": w.is_alive}
                    for w in metrics.workers
                ],
                "anomalies": metrics.stuck_tasks
            }
        
        self.logger.info("metrics_collected", mode="local", payload=payload)
    
    def check_once(self) -> SystemMetrics:
        """Executes one monitoring cycle"""
        metrics = self.collector.collect()
        
        # Always log basic status
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
        
        # Local mode: just log metrics
        if self.config.local_mode:
            self._log_metrics_locally(metrics)
            return metrics
        
        # API mode: send metrics
        if self.api_client:
            success = self.api_client.send_metrics(metrics)
            
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
            self.logger.warning("No API key configured - metrics are only logged locally")
            self._log_metrics_locally(metrics)
        
        return metrics
    
    def run(self):
        """Runs the main agent loop"""
        mode = "local" if self.config.local_mode else "api"
        
        self.logger.info(
            "Doorman Agent starting",
            version=AGENT_VERSION,
            mode=mode,
            check_interval=self.config.check_interval_seconds,
            monitored_queues=self.config.monitored_queues,
            api_url=self.config.api_url if not self.config.local_mode else "disabled"
        )
        
        # Validate API key if in API mode
        if not self.config.local_mode and self.api_client:
            self.logger.info("Validating API key...")
            if not self.api_client.validate_api_key():
                self.logger.error("API key validation failed. Please check your DOORMAN_API_KEY.")
                sys.exit(1)
            self.logger.info("API key validated successfully")
        elif self.config.local_mode:
            self.logger.info("Running in LOCAL MODE - metrics will be logged, not sent to API")
        
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
