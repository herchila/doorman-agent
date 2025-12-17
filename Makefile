.PHONY: help clean

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
