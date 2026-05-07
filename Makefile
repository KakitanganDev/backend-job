PYTHON ?= python3

.PHONY: install run test lint clean

install:
	$(PYTHON) -m pip install -r requirements.txt

run:
	uvicorn src.app:app --reload --host 0.0.0.0 --port 8000

test:
	$(PYTHON) -m pytest tests/ -v

lint:
	$(PYTHON) -m ruff check src/ tests/

clean:
	rm -rf __pycache__ .pytest_cache src/__pycache__ tests/__pycache__
