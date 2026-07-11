# Makefile for C-Call IVR Platform development
.PHONY: dev test lint migrate shell logs clean backup help

# Default help message
help:
	@echo "Available commands:"
	@echo "  make dev      - Start application stack locally using Docker Compose"
	@echo "  make test     - Run all unit and integration tests using pytest"
	@echo "  make lint     - Automatically lint and format codebase using Ruff"
	@echo "  make migrate  - Run all database migrations"
	@echo "  make shell    - Open a Flask shell in the running app container"
	@echo "  make logs     - Follow application logs from Docker Compose"
	@echo "  make clean    - Stop services and remove all associated data volumes"
	@echo "  make backup   - Backup codebase zip and database SQL/DB dump"

dev:
	docker compose up

test:
	.venv/Scripts/pytest tests/ -v || pytest tests/ -v

lint:
	ruff check . --fix && ruff format .

migrate:
	python migrations/run_migrations.py

shell:
	docker compose exec app flask shell

logs:
	docker compose logs -f app

clean:
	docker compose down -v

backup:
	.venv/Scripts/python.exe backup.py || python backup.py
