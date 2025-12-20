from src.doorman_agent.celery_doorman import Config
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
        # import ipdb; ipdb.set_trace()
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
        # import ipdb; ipdb.set_trace()
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
