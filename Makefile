.PHONY: install dev run format

install:
	uv sync

dev:
	fastapi dev backend/app.py

run:
	fastapi run backend/app.py

format:
	black .
	isort .