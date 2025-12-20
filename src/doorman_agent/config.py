from dataclasses import field
from typing import List, Optional


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