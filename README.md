# 🚪 Doorman Agent

**Lightweight monitoring agent for Celery/Redis queues.**

Doorman Agent collects metrics from your Celery workers and Redis queues, then sends them to [doorman.com](https://doorman.com) for analysis and alerting.

## Requirements

- Python 3.10+
- Redis
- Celery

## Installation

```bash
pip install doorman-agent
```

For better Redis performance:

```bash
pip install doorman-agent[performance]  # includes hiredis
```

## Quick Start

### API Mode (Production)

```bash
# Set your API key
export DOORMAN_API_KEY=your-api-key

# Run the agent
doorman-agent --config config.yaml
```

### Local Mode (Testing)

```bash
# Run without API - metrics are logged to stdout
doorman-agent --config config.yaml --local
```

## Configuration

Create a `config.yaml` file:

```yaml
# API Connection
api_key: null  # Use DOORMAN_API_KEY env var instead
api_url: "https://api.doorman.com"

# Local mode (no API calls, just logging)
local_mode: false

# Redis/Celery
redis_url: "redis://localhost:6379/0"
celery_broker_url: "redis://localhost:6379/0"

# Behavior
check_interval_seconds: 30

# Queues to monitor
monitored_queues:
  - celery
  - default
  - emails
  - payments
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DOORMAN_API_KEY` | Your doorman.com API key | Required (API mode) |
| `DOORMAN_API_URL` | API endpoint | `https://api.doorman.com` |
| `DOORMAN_LOCAL_MODE` | Set to `true` for local mode | `false` |
| `REDIS_URL` | Redis connection URL | `redis://localhost:6379/0` |
| `CELERY_BROKER_URL` | Celery broker URL | `redis://localhost:6379/0` |
| `CHECK_INTERVAL` | Seconds between checks | `30` |

## CLI Options

```bash
doorman-agent --help

Options:
  --config, -c    Path to YAML configuration file
  --once, -1      Run only once (for testing)
  --local, -l     Local mode: only log metrics, no API calls
  --api-key, -k   Doorman API key
  --api-url       Doorman API URL
  --version, -v   Show version
```

## Output Modes

### API Mode
Metrics are sent to doorman.com where they're analyzed and alerts are triggered based on your configured thresholds.

### Local Mode (`--local`)
Metrics are logged as structured JSON to stdout. Perfect for:
- Testing the agent before connecting to API
- Analyzing metrics locally
- Piping to your own logging system

Example local mode output:
```json
{
  "timestamp": "2025-12-17T10:30:00Z",
  "level": "INFO",
  "message": "metrics_collected",
  "mode": "local",
  "payload": {
    "metrics": {
      "total_pending": 150,
      "total_active": 8,
      "total_workers": 4,
      "alive_workers": 4
    },
    "infra_health": {
      "redis": true,
      "celery": true
    },
    "queues": [...],
    "workers": [...],
    "anomalies": []
  }
}
```

## Metrics Collected

| Metric | Description |
|--------|-------------|
| `total_pending` | Total tasks waiting in all queues |
| `total_active` | Tasks currently being processed |
| `total_workers` | Registered Celery workers |
| `alive_workers` | Workers responding to ping |
| `queues[].depth` | Pending tasks per queue |
| `queues[].latency_sec` | Age of oldest task in queue |
| `workers[].status` | online, offline, stuck |
| `anomalies[]` | Stuck tasks (>30 min runtime) |

## Privacy

The agent is designed with privacy in mind:
- Worker hostnames are hashed (`celery@prod-worker-1` → `w-a1b2c3d4`)
- Queue names are sanitized (emails/UUIDs redacted)
- Task arguments are **never** collected

## Development

```bash
# Clone the repository
git clone https://github.com/doorman/doorman-agent.git
cd doorman-agent

# Install with dev dependencies
poetry install --with dev

# Run linting and type checking
pre-commit run --all-files

# Run tests
poetry run pytest
```

## License

MIT
