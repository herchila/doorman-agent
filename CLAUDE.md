# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Doorman Agent is a lightweight monitoring agent for Celery/Redis queues. It collects metrics from Celery workers and Redis queues, then either:
- **API mode**: Sends metrics to doorman.com for analysis and alerting
- **Local mode**: Logs metrics as structured JSON to stdout (no API calls)

**Python version**: 3.9+ (supports up to 3.13)
**Package manager**: Poetry

## Development Commands

### Setup
```bash
# Install with dev dependencies
poetry install --with dev

# Install pre-commit hooks
pre-commit install
```

### Testing
```bash
# Run all tests
poetry run pytest

# Run tests with coverage
poetry run pytest --cov=doorman_agent --cov-report=html

# Run a single test file
poetry run pytest tests/test_config.py

# Run a specific test
poetry run pytest tests/test_config.py::TestLoadConfigDefaults::test_default_api_url
```

### Linting and Type Checking
```bash
# Run all pre-commit hooks (ruff, mypy, etc.)
pre-commit run --all-files

# Run ruff linter only
ruff check .

# Run ruff with auto-fix
ruff check --fix .

# Run ruff formatter
ruff format .

# Run mypy type checker
mypy src/doorman_agent
```

### Running the Agent
```bash
# Run in local mode (no API calls, just logging)
poetry run doorman-agent --config config.yaml --local

# Run once (for testing)
poetry run doorman-agent --config config.yaml --once

# Run in API mode (production)
export DOORMAN_API_KEY=your-api-key
poetry run doorman-agent --config config.yaml
```

## Architecture

### Core Components

1. **DoormanAgent** (`agent.py`)
   - Main orchestrator that runs the monitoring loop
   - Manages two modes: API mode (sends to doorman.com) and local mode (logs only)
   - Handles graceful shutdown via SIGTERM/SIGINT
   - Tracks consecutive API failures and continues collecting even if API is down

2. **MetricsCollector** (`collector.py`)
   - Collects metrics from Redis (queue depths, task ages) and Celery (worker stats, active tasks)
   - **Auto-discovers queues**: If `monitored_queues` is empty in config, it discovers queues from Celery workers via `inspector.active_queues()`
   - Detects stuck tasks (tasks running longer than `max_task_runtime_seconds` threshold)
   - Returns `SystemMetrics` containing queue, worker, and anomaly data

3. **APIClient** (`api_client.py`)
   - Sends metrics to doorman.com API (production mode)
   - **Privacy-first design**:
     - Hashes worker hostnames (`celery@prod-worker-1` → `w-a1b2c3d4`)
     - Sanitizes task signatures (removes UUIDs, emails, numeric IDs)
     - Sanitizes queue names (redacts emails/UUIDs)
     - Never collects task arguments
   - Validates API key on startup
   - Uses stdlib `urllib` (no external HTTP library dependency)

4. **Config** (`config.py`, `models.py`)
   - Loads from YAML file + environment variables (env vars override YAML)
   - Pydantic models for validation
   - If `monitored_queues` is empty, the agent auto-discovers queues from workers

### Data Flow

```
MetricsCollector.collect()
  → connects to Redis + Celery
  → discovers queues if monitored_queues is empty
  → gets queue depths from Redis (LLEN)
  → gets oldest task age from Redis (LINDEX -1)
  → inspects workers (active, reserved, stats)
  → detects stuck tasks
  → returns SystemMetrics

DoormanAgent.check_once()
  → calls collector.collect()
  → logs basic status
  → if local_mode: logs full metrics as JSON
  → if API mode: sends to APIClient.send_metrics()
    → APIClient.build_payload() applies privacy transformations
    → POST to /api/v1/metrics
```

### Key Design Patterns

- **Privacy by default**: All worker names, task IDs, and queue names are hashed/sanitized before sending to API
- **Graceful degradation**: Agent continues collecting metrics even if API calls fail
- **Auto-discovery**: Queues are auto-discovered from workers if not configured (reduces config boilerplate)
- **Two modes**: Local mode for testing/debugging without API, API mode for production monitoring

## Configuration System

Configuration loads in this order (later overrides earlier):
1. YAML file defaults
2. Environment variables

**Important**: If `monitored_queues` is not set in config, queues are auto-discovered from Celery workers using `control.inspect().active_queues()`.

## Privacy and Sanitization

The agent is designed with **privacy-first principles**:

- Worker names are hashed: `_hash_worker_id()` produces `w-a1b2c3d4` format
- Task IDs are hashed: `_hash_task_id()` produces `t-a1b2c3d4e5f6` format
- Task signatures are sanitized by `_sanitize_task_signature()`:
  - Emails: `send_to_john@example.com` → `send_to_[email]`
  - UUIDs: `order_550e8400-e29b-41d4-a716-446655440000` → `order_[uuid]`
  - Numeric IDs: `process_user_12345` → `process_user_[id]`
- Queue names are sanitized by `_sanitize_queue_name()` (removes emails/UUIDs)
- Task arguments are **never** accessed or collected

Privacy sanitization can be disabled per-task-signature via `privacy.sanitize_task_signatures: false` in config.

## Testing Notes

- Tests use `pytest` with fixtures
- `monkeypatch` is used to set/unset environment variables
- Config tests verify YAML loading, env var overrides, and defaults
- The project uses parametrized tests for testing multiple input variations (see `test_config.py`)

## Code Style

- **Linter**: Ruff (replaces flake8, black, isort)
- **Type checker**: mypy
- **Line length**: 100 characters (configured in pyproject.toml)
- **Import style**: Ruff handles import sorting
- Pre-commit hooks enforce all style rules automatically
