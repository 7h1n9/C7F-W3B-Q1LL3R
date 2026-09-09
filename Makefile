.PHONY: install db-up migrate backend frontend runner test lint
install:
	cd backend && pip install -e ".[dev]"
	cd kali-runner && pip install -e ".[dev]"
	cd frontend && npm install
db-up:
	docker compose up -d mysql
migrate:
	cd backend && alembic upgrade head
backend:
	cd backend && uvicorn app.main:app --reload --port 8000
frontend:
	cd frontend && npm run dev
runner:
	cd kali-runner && uvicorn app.main:app --port 8091
test:
	cd backend && pytest
	cd kali-runner && pytest
lint:
	cd backend && ruff check .
	cd kali-runner && ruff check .
