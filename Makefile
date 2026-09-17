.PHONY: all clean test ci ci-strict ci-base lint lint-tests docs cov open-coverage virt virt-meshtasticd virt-smokevirt-meshtasticd simradio smoke1 smoke1-destructive slow install examples protobufs protobufs-update api-baseline api-baseline-master FORCE

POETRY_RUN := poetry run
API_BASELINE_FILE := meshtastic/tests/api_baselines/api_baseline.json
API_BASELINE_REF ?= upstream/master

all: test

clean:
	rm -rf htmlcov .coverage coverage.xml .mypy_cache .pytest_cache dist build *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

# only run the fast unit tests
test:
	$(POETRY_RUN) pytest -m unit

# run baseline CI checks locally
# Runs the first CI stages in order:
# pytest (with coverage) -> pylint -> ruff (tests)
ci-base:
	$(POETRY_RUN) pytest --cov=meshtastic --cov-report=xml
	$(MAKE) lint
	$(MAKE) lint-tests

ci:
	$(MAKE) ci-base
	$(POETRY_RUN) mypy meshtastic/

# generate API baseline from current working tree
api-baseline:
	$(POETRY_RUN) python bin/extract_api_surface.py meshtastic > $(API_BASELINE_FILE)

# generate API baseline from the upstream source snapshot (upstream/master;
# add the remote first if needed: git remote add upstream
# https://github.com/meshtastic/python.git)
api-baseline-master:
	./bin/generate_master_api_baseline.sh "$(API_BASELINE_REF)"

# run CI checks with strict mypy (for maintainers)
ci-strict:
	$(MAKE) ci-base
	$(POETRY_RUN) mypy meshtastic/ --strict

# only run the smoke tests against the virtual device
virt:
	$(POETRY_RUN) pytest -m smokevirt

# run meshtasticd simulator integration tests (defaults to test_meshtasticd_ci.py + test_meshtasticd_tcp_interface_ci.py unless MESHTASTICD_PYTEST_TARGETS is set)
virt-meshtasticd:
	./bin/run-smokevirt-with-meshtasticd.sh

# run the full legacy smokevirt suite against meshtasticd simulator container
virt-smokevirt-meshtasticd:
	MESHTASTICD_PYTEST_TARGETS="meshtastic/tests/test_smokevirt.py" \
	MESHTASTICD_PYTEST_MARK_EXPR="smokevirt and not smoke1_destructive" \
	./bin/run-smokevirt-with-meshtasticd.sh

# run process-managed native meshtasticd single/multi-node smoke tests
simradio:
	$(POETRY_RUN) pytest -m simradio -v --durations=20

# run stable non-destructive smoke1 hardware checks
smoke1:
	$(POETRY_RUN) pytest -m "smoke1 and not smoke1_destructive" -s -vv

# run destructive smoke1 hardware checks (reboot/reset/config mutation)
smoke1-destructive:
	$(POETRY_RUN) pytest -m "smoke1 and smoke1_destructive" -s -vv

# local install
install:
	poetry install

# generate the docs (for local use)
# -d numpy: project docstrings use numpy-style parameter sections
# --no-search: pdoc 16.0.0's Node-based search-index build is broken upstream
# (CommonJS require in an ESM-scoped package) and fails the run; drop search.
# !meshtastic\.protobuf: pdoc 16.0.0 cannot evaluate the generated
# mypy-protobuf .pyi stubs (they reference private protobuf internals removed
# in protobuf 6) and spews per-module stub-parsing errors; excluding the
# generated modules keeps the run clean.
docs:
	$(POETRY_RUN) pdoc --no-search -d numpy --output-dir docs meshtastic '!meshtastic\.protobuf'

# lint the codebase (same command as CI)
lint:
	PYLINTHOME=$${TMPDIR:-/tmp}/pylint-cache $(POETRY_RUN) pylint meshtastic examples/

# lint tests with the canonical Ruff version (same scope as CI)
lint-tests:
	.trunk/trunk check --filter=ruff meshtastic/tests tests

# show the slowest unit tests
slow:
	$(POETRY_RUN) pytest -m unit --durations=5

protobufs: FORCE
	git submodule update --init --recursive
	./bin/regen-protobufs.sh

protobufs-update: FORCE
	git submodule update --init --recursive
	git submodule update --remote --merge
	./bin/regen-protobufs.sh

# run the coverage report and open results in a browser
open-coverage:
	@# Open report when possible; otherwise print location.
	@if command -v open >/dev/null 2>&1; then \
		open htmlcov/index.html >/dev/null 2>&1 || echo "Coverage report generated at htmlcov/index.html"; \
	elif command -v xdg-open >/dev/null 2>&1; then \
		xdg-open htmlcov/index.html >/dev/null 2>&1 || echo "Coverage report generated at htmlcov/index.html"; \
	else \
		echo "Coverage report generated at htmlcov/index.html"; \
	fi

cov:
	$(POETRY_RUN) pytest --cov-report html --cov=meshtastic
	@$(MAKE) open-coverage

# run cli examples
examples: FORCE
	$(POETRY_RUN) pytest -m examples

# Makefile hack to get the examples to always run
FORCE: ;
