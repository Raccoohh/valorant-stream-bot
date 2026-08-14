.PHONY: test test-local up down logs

# Run the test suite inside the app container (no db/redis needed —
# the parser tests are pure in-memory contract tests).
test:
	docker compose run --rm --no-deps app python -m pytest tests -v

# Run tests on the host (requires: pip install -r requirements.txt)
test-local:
	python -m pytest tests -v

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f app
