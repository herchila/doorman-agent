# 🚪 Celery Doorman

**Silent Queue Guardian** - A lightweight process that monitors Redis/Celery status and triggers early alerts via webhooks when anomalies are detected.

## 🎯 What Problem Does It Solve?

> "The user didn't receive the confirmation email 2 hours ago..."

Celery Doorman detects problems BEFORE your users report them:

- **Backlog Explosion**: Thousands of tasks piling up
- **Silent Death**: Workers down while the queue grows
- **Zombie Tasks**: Stuck tasks that never finish
- **High Latency**: Tasks waiting too long in queue

## 📊 Monitored Metrics

| Metric | Description | Why It Matters |
|--------|-------------|----------------|
| **Queue Depth** | Pending tasks per queue | Detects accumulation |
| **Latency** | Age of oldest task | "How long is the user waiting?" |
| **Worker Heartbeat** | Are workers alive? | Detects silent crashes |
| **Active Tasks** | Currently running tasks | Detects zombies |

## 🚨 Alert Scenarios

| Scenario | Status | Severity |
|----------|--------|----------|
| Workers down + empty queue | ✅ Detects | ⚠️ Warning |
| Workers down + queue filling | ✅ Detects | 🔴 Emergency |
| Backlog explosion (>1000 tasks) | ✅ Ready | 🔴 Critical |
| Zombie tasks (>30 min running) | ✅ Ready | ⚠️ Warning |
| High latency in critical queue | ✅ Ready | 🔴 Critical |

---

## 🚀 Quick Start

### Installation

```bash
pip install redis celery pyyaml
```

### Simulation Mode (Recommended for Testing)

No external Redis or Celery needed! The simulation mode starts everything locally:

```bash
# Healthy scenario: 1 worker running
python celery_doorman.py --simulate --workers 1

# Workers down (WARNING alert)
python celery_doorman.py --simulate --workers 0

# EMERGENCY: Workers down + tasks pending
python celery_doorman.py --simulate --workers 0 --enqueue 5

# Backlog scenario: many tasks accumulating
python celery_doorman.py --simulate --workers 1 --enqueue 50
```

**Requirements for simulation mode:**
- `redis-server` installed locally (`apt install redis-server` or `brew install redis`)

### Production Mode

```bash
# Configure
cp config.example.yaml config.yaml
# Edit config.yaml with your REDIS_URL and SLACK_WEBHOOK_URL

# Run as daemon
python celery_doorman.py --config config.yaml

# Single check (for cron)
python celery_doorman.py --config config.yaml --once
```

### Docker

```bash
# Simulation mode
docker-compose run --rm doorman --simulate --workers 1
docker-compose run --rm doorman --simulate --workers 0 --enqueue 5

# Production mode (uses Redis from docker-compose)
docker-compose up -d
```

---

## 🎬 Demo: Simulation Mode

Run these commands to see Doorman in action. Each scenario demonstrates a different alert type.

### ✅ Scenario 1: Healthy System

```bash
python celery_doorman.py --simulate --workers 1
```

**Output:**
```
🧪 CELERY DOORMAN - SIMULATION MODE
   Workers requested: 1
   Tasks to enqueue:  0

📦 Starting Redis server...
✅ Redis started on port 6399
👷 Starting 1 Celery worker(s)...
✅ 1 worker(s) started

🚪 Starting Doorman monitoring...

--- Health Check #1 ---
   📊 Pending tasks:  0
   👷 Alive workers:  1/1
   🔗 Redis:          ✅
   🔗 Celery:         ✅
```

**Result**: No alerts. System healthy. ✅

---

### ⚠️ Scenario 2: Workers Down (No Pending Tasks)

```bash
python celery_doorman.py --simulate --workers 0
```

**Output:**
```
🧪 CELERY DOORMAN - SIMULATION MODE
   Workers requested: 0
   Tasks to enqueue:  0

⚠️  No workers started (simulating failure scenario)

--- Health Check #1 ---
   📊 Pending tasks:  0
   👷 Alive workers:  0/0
   🔗 Redis:          ✅
   🔗 Celery:         ❌
```

```json
{
  "severity": "warning",
  "alert_type": "celery_disconnected",
  "title": "⚠️ Celery Inspect Failed"
}
```

**Result**: Warning alert triggered. ⚠️

---

### 🔴 Scenario 3: EMERGENCY - Workers Down + Queue Filling

```bash
python celery_doorman.py --simulate --workers 0 --enqueue 5
```

**Output:**
```
🧪 CELERY DOORMAN - SIMULATION MODE
   Workers requested: 0
   Tasks to enqueue:  5

⚠️  No workers started (simulating failure scenario)
📝 Enqueuing 5 task(s)...
✅ 5 task(s) enqueued

--- Health Check #1 ---
   📊 Pending tasks:  5
   👷 Alive workers:  0/0
   🔗 Redis:          ✅
   🔗 Celery:         ❌
```

```json
{
  "severity": "emergency",
  "alert_type": "workers_down",
  "title": "🔴 EMERGENCY: All Workers Down",
  "message": "No workers responding but 5 tasks are pending!"
}
```

**Result**: EMERGENCY alert triggered! 🔴

---

### 📊 Scenario 4: Backlog Building Up

```bash
python celery_doorman.py --simulate --workers 1 --enqueue 20
```

With `max_queue_size: 10` (simulation default), this triggers a backlog alert.

```json
{
  "severity": "critical",
  "alert_type": "backlog_explosion",
  "title": "🚨 Backlog Explosion Detected",
  "message": "Total pending tasks (20) exceeds threshold (10)"
}
```

**Result**: CRITICAL alert triggered! 🔴

---

## ⚙️ Configuration

### Environment Variables

```bash
# Connections
REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/0

# Webhooks
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
PAGERDUTY_ROUTING_KEY=your-routing-key
GENERIC_WEBHOOK_URL=https://your-endpoint.com/webhook

# Thresholds
CHECK_INTERVAL=30
MAX_QUEUE_SIZE=1000
MAX_WAIT_TIME=60
```

### YAML File

```yaml
redis_url: "redis://redis:6379/0"
slack_webhook_url: "https://hooks.slack.com/..."

thresholds:
  max_queue_size: 1000
  max_wait_time_seconds: 60
  max_task_runtime_seconds: 1800
  critical_queues:
    - payments
    - emails

monitored_queues:
  - celery
  - payments
  - emails
  - notifications
```

---

## 📤 Output

### Structured Logs (JSON to stdout)

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "level": "INFO",
  "message": "Health check completed",
  "pending_tasks": 150,
  "active_tasks": 8,
  "alive_workers": 4,
  "redis_connected": true
}
```

### Slack Notification

```
🚨 Backlog Explosion Detected

Total pending tasks (5000) exceeds threshold (1000)

| Field | Value |
|-------|-------|
| total_pending | 5000 |
| threshold | 1000 |
| queues.payments | 2000 |
| queues.emails | 3000 |
```

### PagerDuty Event

Sent as v2 event with automatically mapped severity.

---

## 🔧 Execution Modes

```bash
# Daemon (infinite loop)
python celery_doorman.py --config config.yaml

# Single run (for cron)
python celery_doorman.py --config config.yaml --once

# Dry run (no alerts sent, only logging)
python celery_doorman.py --config config.yaml --dry-run
```

---

## 🐳 Production Deployment

### Systemd

```bash
sudo cp celery-doorman.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable celery-doorman
sudo systemctl start celery-doorman

# View logs
sudo journalctl -u celery-doorman -f
```

### Kubernetes

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: celery-doorman
spec:
  replicas: 1  # Only need 1
  template:
    spec:
      containers:
      - name: doorman
        image: celery-doorman:latest
        env:
        - name: REDIS_URL
          valueFrom:
            secretKeyRef:
              name: redis-credentials
              key: url
        - name: SLACK_WEBHOOK_URL
          valueFrom:
            secretKeyRef:
              name: slack-webhook
              key: url
        resources:
          requests:
            memory: "64Mi"
            cpu: "50m"
          limits:
            memory: "128Mi"
            cpu: "100m"
```

---

## 🔒 Alert Cooldown

To avoid alert spam, each alert type has a configurable cooldown (default: 5 minutes). The same alert won't repeat until the cooldown expires.

```yaml
alert_cooldown_seconds: 300  # 5 minutes
```

---

## 🌐 Network Considerations

### Simulation Mode (Easiest)

No network configuration needed! Simulation mode starts its own Redis on port `6399`:

```bash
python celery_doorman.py --simulate --workers 1
```

### Production Mode - Same Docker Network

When monitoring a real Celery deployment, Doorman must be in the **same network** as Redis/Celery:

```
┌─────────────────────────────────────────────┐
│  Network: your-project_default              │
│                                             │
│  ┌─────────┐  ┌──────────────┐  ┌─────────┐ │
│  │  Redis  │  │ CeleryWorker │  │ Doorman │ │
│  └─────────┘  └──────────────┘  └─────────┘ │
│       ↑              ↑              ↑       │
│       └──────────────┴──────────────┘       │
│              All can communicate            │
└─────────────────────────────────────────────┘
```

**Option 1**: Add Doorman to your existing `docker-compose.yml`

**Option 2**: Run standalone with `--network`:
```bash
docker run --rm -it \
  --network your-project_default \
  -v "$(pwd)":/app \
  -w /app \
  celery-doorman --config config.yaml
```

**Option 3**: In production (VPC, Kubernetes) - ensure network connectivity between Doorman and Redis.

---

## 📈 Roadmap

- [ ] Simple web dashboard (historical metrics)
- [ ] Prometheus/Grafana integration
- [ ] Auto-scaling workers based on metrics
- [ ] RabbitMQ support (in addition to Redis)
- [ ] HTTP healthcheck endpoint

---

## 🆕 Features

- **New metrics**: Extend `MetricsCollector`
- **New alerts**: Add scenarios in `RiskAnalyzer`
- **New destinations**: Add methods in `Notifier`

---

**Doorman**: Because production problems shouldn't be a surprise. 🚪
