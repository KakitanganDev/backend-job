.PHONY: install run test lint clean db-up db-down db-migrate db-revision

install:
	pip install -r requirements.txt

run:
	uvicorn src.app:app --reload --host 0.0.0.0 --port 8000

test:
	python -m pytest tests/ -v

lint:
	ruff check src tests
	ruff format --check src tests

fmt:
	ruff format src tests

db-up:
	docker compose up -d db
	@echo "Waiting for Postgres to be ready..."
	@until docker compose exec -T db pg_isready -U kakitangan -d kakitangan >/dev/null 2>&1; do sleep 0.5; done
	@echo "Postgres ready."

db-down:
	docker compose down

db-migrate:
	alembic upgrade head

db-revision:
	alembic revision --autogenerate -m "$(m)"

clean:
	rm -rf __pycache__ .pytest_cache *.db src/__pycache__ tests/__pycache__ \
		.ruff_cache htmlcov .coverage
