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
