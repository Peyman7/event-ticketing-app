COMPOSE ?= docker compose
RUN := $(COMPOSE) run --rm web

.PHONY: help build up down logs migrate makemigrations shell test lint fmt seed explain loadtest check-inventory

help:  ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-16s %s\n", $$1, $$2}'

build:  ## Build images
	$(COMPOSE) build

up:  ## Start db, web, worker
	$(COMPOSE) up -d db web worker

down:  ## Stop everything
	$(COMPOSE) down

logs:  ## Tail web + worker logs
	$(COMPOSE) logs -f web worker

migrate:  ## Apply migrations
	$(RUN) python manage.py migrate

makemigrations:  ## Create migrations
	$(RUN) python manage.py makemigrations

shell:  ## Django shell
	$(RUN) python manage.py shell

test:  ## Run the test suite against Postgres
	$(RUN) pytest $(ARGS)

lint:  ## Ruff lint + format check
	$(RUN) ruff check .
	$(RUN) ruff format --check .

fmt:  ## Auto-format
	$(RUN) ruff check --fix .
	$(RUN) ruff format .

seed:  ## Seed demo data (ARGS="--tickets 100000" for EXPLAIN data)
	$(RUN) python manage.py seed_demo $(ARGS)

explain:  ## Run EXPLAIN ANALYZE on the main queries
	$(RUN) python manage.py explain_queries

check-inventory:  ## Verify reserved_count matches held+sold rows
	$(RUN) python manage.py check_inventory

loadtest:  ## Headless on-sale spike (see docs/load-test.md)
	$(COMPOSE) --profile loadtest run --rm locust -f locustfile.py --headless \
	  --host http://web:8000 -u $${USERS:-300} -r $${SPAWN:-50} -t $${DURATION:-60s} \
	  --csv results/onsale --html results/onsale.html
