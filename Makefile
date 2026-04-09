.PHONY: install dev run

install:
	uv sync

dev:
	fastapi dev backend/app.py

run:
	fastapi run backend/app.py
