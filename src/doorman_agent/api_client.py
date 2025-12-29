"""
API Client for communicating with doorman.com
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from doorman_agent.logger import StructuredLogger
from doorman_agent.models import Config, SystemMetrics

AGENT_VERSION = "0.1.0"


class APIClient:
    """
    Client for communicating with doorman.com API.
    The agent only collects and sends metrics - the API handles analysis and notifications.
    """

    DEFAULT_API_URL = "https://api.doorman.com"

    def __init__(
        self,
        api_key: str,
        api_url: Optional[str] = None,
        logger: Optional[StructuredLogger] = None,
        config: Optional[Config] = None,
    ):
        self.api_key = api_key
        self.api_url = (api_url or self.DEFAULT_API_URL).rstrip("/")
        self.logger = logger or StructuredLogger("doorman-api-client")
        self.config = config
        self._session_id = self._generate_session_id()

    def _generate_session_id(self) -> str:
        """Generate a unique session ID for this agent instance"""
        unique_string = f"{platform.node()}-{os.getpid()}-{time.time()}"
        return hashlib.sha256(unique_string.encode()).hexdigest()[:16]

    def _hash_worker_id(self, worker_name: str) -> str:
        """Generate a privacy-safe hash for worker identification"""
        return "w-" + hashlib.sha256(worker_name.encode()).hexdigest()[:8]

    def _hash_task_id(self, task_id: str) -> str:
        """Generate a privacy-safe hash for task identification"""
        return "t-" + hashlib.sha256(task_id.encode()).hexdigest()[:12]

    def _sanitize_display_name(self, worker_name: str) -> str:
        """
        Extract clean display name from worker hostname.
        'celery@worker-1.prod.internal' -> 'worker-1'
        'celery@ip-10-0-1-234' -> 'ip-10-0-1-234'
        """
        # Remove celery@ prefix
        if "@" in worker_name:
            worker_name = worker_name.split("@", 1)[1]

        # Remove domain suffix
        if "." in worker_name:
            worker_name = worker_name.split(".")[0]

        return worker_name

    def _sanitize_task_signature(self, task_name: str) -> str:
        """
        Sanitize task signature to remove potential PII.
        
        Examples:
            'app.tasks.send_email' -> 'app.tasks.send_email' (no change)
            'app.tasks.process_user_12345' -> 'app.tasks.process_user_[id]'
            'app.tasks.send_to_john@example.com' -> 'app.tasks.send_to_[email]'
            'app.tasks.order_550e8400-e29b-41d4-a716-446655440000' -> 'app.tasks.order_[uuid]'
        """
        # Check if sanitization is disabled
        if self.config and not self.config.privacy.sanitize_task_signatures:
            return task_name

        # Pattern for email addresses
        email_pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
        sanitized = re.sub(email_pattern, "[email]", task_name)

        # Pattern for UUIDs
        uuid_pattern = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        sanitized = re.sub(uuid_pattern, "[uuid]", sanitized, flags=re.IGNORECASE)

        # Pattern for numeric IDs (sequences of 4+ digits, likely user/order IDs)
        # Only replace if preceded by underscore, hyphen, or word boundary
        numeric_id_pattern = r"(?<=[_\-])\d{4,}(?=[_\-\s]|$)"
        sanitized = re.sub(numeric_id_pattern, "[id]", sanitized)

        # Pattern for numeric suffix at end of task name
        numeric_suffix_pattern = r"_\d{4,}$"
        sanitized = re.sub(numeric_suffix_pattern, "_[id]", sanitized)

        return sanitized

    def _sanitize_queue_name(self, queue_name: str) -> str:
        """
        Sanitize queue name to remove potential PII.
        Detects email patterns and replaces them.
        """
        # Pattern for email addresses
        email_pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
        sanitized = re.sub(email_pattern, "[email]", queue_name)

        # Pattern for UUIDs (might contain user IDs)
        uuid_pattern = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        sanitized = re.sub(uuid_pattern, "[uuid]", sanitized, flags=re.IGNORECASE)

        return sanitized

    def _worker_status(self, is_alive: bool, worker_name: str, stuck_worker_refs: set) -> str:
        """Determine worker status"""
        if not is_alive:
            return "offline"
        if worker_name in stuck_worker_refs:
            return "stuck"
        return "online"

    def _get_headers(self) -> Dict[str, str]:
        """Returns headers for API requests"""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"doorman-agent/{AGENT_VERSION}",
            "X-Agent-Session": self._session_id,
        }

    def _make_request(
        self, method: str, endpoint: str, payload: Optional[Dict] = None
    ) -> tuple[bool, Optional[Dict]]:
        """Makes HTTP request to the API"""
        url = f"{self.api_url}{endpoint}"

        try:
            data = json.dumps(payload).encode("utf-8") if payload else None
            req = urllib.request.Request(
                url, data=data, headers=self._get_headers(), method=method
            )

            with urllib.request.urlopen(req, timeout=30) as response:
                response_data = json.loads(response.read().decode("utf-8"))
                return True, response_data

        except urllib.error.HTTPError as e:
            error_body = None
            try:
                error_body = json.loads(e.read().decode("utf-8"))
            except Exception:
                pass

            self.logger.error(
                "API request failed",
                endpoint=endpoint,
                status_code=e.code,
                error=error_body or str(e),
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
                plan=response.get("plan", "unknown"),
            )
            return True

        return False

    def build_payload(self, metrics: SystemMetrics) -> Dict[str, Any]:
        """
        Builds privacy-safe payload from metrics.
        Can be used for both API sending and local logging.
        """
        # Build worker ID hash lookup for anomalies references
        worker_hash_lookup = {}
        stuck_worker_refs = set()

        # First pass: identify workers with stuck tasks
        for stuck in metrics.stuck_tasks:
            worker_name = stuck.get("worker", "")
            if worker_name:
                stuck_worker_refs.add(worker_name)

        # Build workers list with privacy-safe identifiers
        workers_payload = []
        for w in metrics.workers:
            id_hash = self._hash_worker_id(w.name)
            worker_hash_lookup[w.name] = id_hash

            status = self._worker_status(w.is_alive, w.name, stuck_worker_refs)

            workers_payload.append(
                {
                    "id_hash": id_hash,
                    "display_name": self._sanitize_display_name(w.name),
                    "active_tasks": w.active_tasks,
                    "concurrency": w.concurrency,
                    "status": status,
                }
            )

        # Build anomalies list with hashed task_id and sanitized signature
        anomalies_payload = []
        for stuck in metrics.stuck_tasks:
            worker_name = stuck.get("worker", "")
            task_id = stuck.get("task_id", "unknown")
            task_name = stuck.get("task_name", "unknown")

            anomalies_payload.append(
                {
                    "type": "stuck_task",
                    "task_id_hash": self._hash_task_id(task_id),
                    "task_signature": self._sanitize_task_signature(task_name),
                    "worker_ref": worker_hash_lookup.get(worker_name, "w-unknown"),
                    "duration_sec": stuck.get("runtime_seconds", 0),
                    "started_at": stuck.get("started_at"),
                    "detected_at": metrics.timestamp,
                }
            )

        # Build final payload
        payload = {
            "timestamp": metrics.timestamp,
            "agent_version": AGENT_VERSION,
            "agent_session": self._session_id,
            "metrics": {
                "total_pending": metrics.total_pending_tasks,
                "total_active": metrics.total_active_tasks,
                "total_workers": metrics.total_workers,
                "alive_workers": metrics.alive_workers,
                "total_concurrency": metrics.total_concurrency,
                "saturation_pct": round(metrics.saturation_pct, 2),
                "max_latency_sec": metrics.max_latency_sec,
            },
            "infra_health": {
                "redis": metrics.redis_connected,
                "celery": metrics.celery_connected,
            },
            "queues": [
                {
                    "name": self._sanitize_queue_name(q.name),
                    "depth": q.depth,
                    "latency_sec": q.oldest_task_age_seconds,
                }
                for q in metrics.queues
            ],
            "workers": workers_payload,
            "anomalies": anomalies_payload,
            "privacy": {
                "args_accessed": False,
            },
        }

        return payload

    def send_metrics(self, metrics: SystemMetrics) -> bool:
        """
        Sends collected metrics to the API using privacy-first schema.
        The API will analyze the metrics and trigger notifications if needed.
        """
        payload = self.build_payload(metrics)

        success, response = self._make_request("POST", "/api/v1/metrics", payload)

        if success:
            self.logger.info(
                "Metrics sent to API",
                total_pending=metrics.total_pending_tasks,
                alive_workers=metrics.alive_workers,
                anomalies_count=len(payload["anomalies"]),
                alerts_triggered=response.get("alerts_triggered", 0) if response else 0,
            )

        return success

    def send_heartbeat(self) -> bool:
        """Sends a heartbeat to indicate the agent is alive"""
        payload = {
            "agent_session": self._session_id,
            "agent_version": AGENT_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        success, _ = self._make_request("POST", "/api/v1/heartbeat", payload)
        return success
