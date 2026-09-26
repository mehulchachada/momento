# Momento — convenience targets. They all call install.sh, which installs
# Momento for your user and only asks for sudo to add missing system packages.
PYTHON ?= python3

.PHONY: help install deps check enable update uninstall purge test lint run clean

help:
	@echo "make install    install missing system packages (asks first), install for"
	@echo "                your user (~/.local) and start the recorder"
	@echo "make deps       only install the missing system packages (asks first)"
	@echo "make check      show which dependencies are installed / missing"
	@echo "make update     download the latest Momento from GitHub and reinstall"
	@echo "make uninstall  remove (keeps settings, buffer and clips)"
	@echo "make purge      remove, including settings and the replay buffer"
	@echo "make test       run the test suite"
	@echo "make lint       byte-compile the package and shellcheck install.sh"
	@echo "make run        run the recorder from the source tree (verbose)"
	@echo "make clean      remove caches and build output"

install:
	./install.sh

deps:
	./install.sh --deps-only

check:
	./install.sh --check

# Kept for old instructions: install already enables the service.
enable: install

update:
	./install.sh --update

uninstall:
	./install.sh --uninstall

purge:
	./install.sh --purge

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
