.PHONY: install run test lint clean

PYTHON := .venv/bin/python
PIP := .venv/bin/pip
UVICORN := .venv/bin/uvicorn

install:
	python3 -m venv .venv
	$(PIP) install -r requirements.txt

run:
	$(UVICORN) src.app:app --reload --host 0.0.0.0 --port 8000

test:
	$(PYTHON) -m pytest tests/ -v

clean:
	rm -rf __pycache__ .pytest_cache *.db src/__pycache__ tests/__pycache__
