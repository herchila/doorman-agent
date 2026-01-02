# Contributing to Doorman Agent

Thanks for your interest in contributing! This document will help you get started.

## Development Setup

### Prerequisites

- Python 3.9+
- [Poetry](https://python-poetry.org/docs/#installation)
- Redis (for integration tests)
- Docker (optional, for testing)

### Clone and Install

```bash
git clone https://github.com/doorman-io/doorman-agent.git
cd doorman-agent

# Install dependencies
poetry install --with dev

# Activate virtual environment
poetry shell

# Install pre-commit hooks
pre-commit install
```

### Running Tests

```bash
# Run all tests
poetry run pytest

# Run with coverage
poetry run pytest --cov=doorman_agent --cov-report=html

# Run specific test file
poetry run pytest tests/test_collector.py -v

# Run tests matching pattern
poetry run pytest -k "test_sanitize"
```

### Code Quality

We use pre-commit hooks to ensure code quality:

```bash
# Run all checks
pre-commit run --all-files

# Run specific hook
pre-commit run ruff --all-files
pre-commit run mypy --all-files
```

#### Tools We Use

| Tool | Purpose |
|------|---------|
| [Ruff](https://github.com/astral-sh/ruff) | Linting + formatting (replaces black, isort, flake8) |
| [mypy](https://mypy.readthedocs.io/) | Static type checking |
| [pytest](https://docs.pytest.org/) | Testing |

### Manual Testing

```bash
# Run agent in local mode (no API calls)
poetry run doorman-agent --local

# Run with custom Redis
REDIS_URL=redis://localhost:6379/1 poetry run doorman-agent --local

# Simulation mode (no real Redis/Celery needed)
poetry run doorman-agent --simulate --workers 2
poetry run doorman-agent --simulate --workers 0 --enqueue 10
```

---

## Coding Guidelines

### Style

- Follow PEP 8 (enforced by Ruff)
- Use type hints everywhere
- Keep functions small and focused
- Write docstrings for public APIs

### Privacy First

**This is critical.** The agent must never collect or transmit PII.

Before adding any new data to the metrics payload:

1. Ask: "Could this contain user data?"
2. If yes, either hash it or don't collect it
3. Add sanitization tests

```python
# ✅ Good - hashed
def _hash_worker_id(self, worker_name: str) -> str:
    return "w-" + hashlib.sha256(worker_name.encode()).hexdigest()[:8]

# ❌ Bad - raw data
def get_worker_id(self, worker_name: str) -> str:
    return worker_name  # Could contain hostnames with PII
```

### Testing

- Write tests for new features
- Test edge cases (empty lists, None values, etc.)
- Test sanitization thoroughly

```python
# Example: test PII sanitization
def test_sanitize_task_signature_with_email():
    client = APIClient(api_key="test")
    result = client._sanitize_task_signature("send_to_john@example.com")
    assert "john" not in result
    assert "[email]" in result
```

---

## Pull Request Process

### 1. Create a Branch

```bash
git checkout -b feature/my-feature
# or
git checkout -b fix/my-bugfix
```

### 2. Make Your Changes

- Write code
- Add tests
- Update documentation if needed

### 3. Run Checks

```bash
# Format and lint
pre-commit run --all-files

# Run tests
poetry run pytest

# Check types
poetry run mypy src/doorman_agent
```

### 4. Commit

We use conventional commits:

```bash
git commit -m "feat: add queue auto-discovery"
git commit -m "fix: handle Redis connection timeout"
git commit -m "docs: update privacy section"
git commit -m "test: add sanitization tests"
```

Prefixes:
- `feat:` New feature
- `fix:` Bug fix
- `docs:` Documentation
- `test:` Tests
- `refactor:` Code refactoring
- `chore:` Maintenance

### 5. Push and Create PR

```bash
git push origin feature/my-feature
```

Then create a Pull Request on GitHub.

### PR Checklist

- [ ] Tests pass locally
- [ ] Pre-commit hooks pass
- [ ] New code has type hints
- [ ] Privacy-sensitive code is reviewed
- [ ] Documentation updated (if applicable)

---

<!-- ## Reporting Security Issues

**Do not open public issues for security vulnerabilities.**

Email security@doorman.com with:
- Description of the vulnerability
- Steps to reproduce
- Potential impact

We'll respond within 48 hours.

---

## Getting Help

- 💬 Discord: [discord.gg/doorman](https://discord.gg/doorman)
- 🐛 Issues: [GitHub Issues](https://github.com/doorman-io/doorman-agent/issues)
- 📧 Email: support@doorman.com

--- -->

## License

By contributing, you agree that your contributions will be licensed under the Apache License 2.0.
