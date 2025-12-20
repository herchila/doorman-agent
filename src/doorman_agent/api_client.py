import json
import os
import time
import urllib.request

from datetime import datetime, timezone
from typing import Dict
from typing import Optional

from src.doorman_agent.models import SystemMetrics
from src.doorman_agent.logger import StructuredLogger

AGENT_VERSION = "0.1.0"


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
            import ipdb; ipdb.set_trace()
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
        
        success, response = self._make_request("POST", "/api/v1/metrics", payload)
        
        import ipdb; ipdb.set_trace()
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
