PYTHON ?= python3

.PHONY: test lint probe check

test: ## shared policy and bootstrap regression cases
	$(PYTHON) -B -m unittest discover -s common/tests -p 'test_*.py'

lint: ## reusable workflow syntax
	go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.7 -shellcheck='' -pyflakes=''

probe: ## actual accepted and rejected Go/SQL lint cases
	$(PYTHON) -B common/probe_lint.py

check: test lint probe
