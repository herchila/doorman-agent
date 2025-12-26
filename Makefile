.PHONY: help clean run_simulate

help:
	@echo "Available commands:"
	@echo "  make clean - Clean temporary files"

clean: ## Clean temporary files
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	find . -name ".coverage" -delete
	find . -name "htmlcov" -exec rm -rf {} +
	find . -name ".pytest_cache" -exec rm -rf {} +
	find . -name ".mypy_cache" -exec rm -rf {} +
	@echo "✅ Temp files removed"

run: ## Run the application
	docker-compose run --rm doorman --config config.yaml

run_once: ## Run the application once
	docker-compose run --rm doorman --config config.yaml --once

run_dry_run: ## Run the application in dry run mode
	docker-compose run --rm doorman --config config.yaml --dry-run

run_simulate: ## Run the application in simulation mode
	docker-compose run --rm doorman --simulate --workers $(workers) --enqueue $(enqueue)
