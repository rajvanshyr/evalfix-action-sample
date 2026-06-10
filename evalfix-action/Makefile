.PHONY: test lint typecheck check smoke

test:
	python -m pytest

lint:
	ruff check .

typecheck:
	mypy --strict src tests scripts

check: lint typecheck test

smoke:
	python scripts/smoke.py
