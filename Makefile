# Convenience targets; `make` lists them.
#
# Only the folder paths are read from .env (not the whole file, which holds the
# Paperless token).

PYTHON     ?= python3
PORT       ?= 8080
SCANS_DIR  ?= $(or $(shell sed -n 's/^SCANS_DIR=//p' .env 2>/dev/null),./scans)
CONFIG_DIR ?= $(or $(shell sed -n 's/^CONFIG_DIR=//p' .env 2>/dev/null),./config)

.DEFAULT_GOAL := help
.PHONY: help test po check run up down restart logs pause build clean

## help      list the targets
help:
	@sed -n 's/^## //p' $(MAKEFILE_LIST)

## test      run the unit tests
test:
	$(PYTHON) -m unittest discover -s tests -t .

## po        collect texts into the translation catalogs (then translate the new ones)
po:
	$(PYTHON) tools/update_po.py

## check     tests + catalogs up to date, as in CI
check: test
	$(PYTHON) tools/update_po.py --check

## run       start locally with the web interface on PORT (real scanner, ~/.config settings)
run:
	$(PYTHON) -m scan_watch --web $(PORT)

## up        build and start the container (creates the scans and config folders)
up:
	mkdir -p "$(SCANS_DIR)" "$(CONFIG_DIR)"
	docker compose up -d --build

## down      stop the container (a stack being scanned is finished first)
down:
	docker compose down

## restart   rebuild and restart the container
restart: up

## logs      follow the container log
logs:
	docker compose logs -f

## pause     pause or resume the running container (SIGUSR1)
pause:
	docker kill -s USR1 scan-watch

## build     build the image only
build:
	docker compose build

## clean     remove caches and build leftovers
clean:
	find . -name __pycache__ -type d -prune -exec rm -r {} +
	rm -rf build *.egg-info

# Release targets (publish, github) of the development repository; not part of
# the GitHub copy.
-include release.mk
