.PHONY: init up down logs check test

init:
	./init.sh

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f dashboard backup-cron backup-monitor

check:
	python3 -m compileall -q backend frontend scripts tests
	@for script in init.sh scripts/*.sh; do bash -n "$$script"; done
	@if [ -f configs/backup.env ]; then docker compose config >/dev/null; else echo "Skipping compose validation: run ./init.sh first"; fi

test:
	python3 -m unittest discover -s tests -v
