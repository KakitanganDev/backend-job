.PHONY: install run test lint clean

install:
	pip install -r requirements.txt

run:
	uvicorn src.app:app --reload --host 0.0.0.0 --port 8000

test: clean
	python -m pytest tests/ -v

clean:
	rm -rf __pycache__ .pytest_cache *.db src/__pycache__ tests/__pycache__
