# Momento — convenience targets. Everything is user-level; nothing here needs root.
PYTHON ?= python3

.PHONY: help install uninstall enable test lint run clean

help:
	@echo "make install    install for the current user (~/.local)"
	@echo "make enable     install, then start now and at every login"
	@echo "make uninstall  remove (keeps settings, buffer and clips)"
	@echo "make test       run the test suite"
	@echo "make lint       byte-compile the package to catch syntax errors"
	@echo "make run        run the recorder from the source tree (verbose)"
	@echo "make clean      remove caches and build output"

install:
	./install.sh

enable:
	./install.sh --enable

uninstall:
	./install.sh --uninstall

test:
	$(PYTHON) -m unittest discover -s tests

lint:
	$(PYTHON) -m compileall -q momento
	@if command -v shellcheck >/dev/null 2>&1; then shellcheck install.sh; fi

# Global options (-v, --config) go before the subcommand.
run:
	$(PYTHON) -m momento -v daemon

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf build dist *.egg-info .pytest_cache
