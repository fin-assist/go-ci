PYTHON ?= python3

.PHONY: test policy-test lint probe check

test: ## shared policy and bootstrap regression cases
	$(PYTHON) -B -m unittest discover -s common/tests -p 'test_*.py'

policy-test: ## Renovate controller regression cases, without credentials or network
	$(PYTHON) -I common/renovate/test.py --root . --mode policy

# actionlint 1.7.12 lacks these GitHub.com features: concurrency.queue, reusable
# job.workflow_sha and repository-relative actions. Source tests verify the
# exact local action paths and use of the resolved workflow SHA.
lint: ## reusable workflow syntax
	go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.12 -shellcheck='' -pyflakes='' \
	  -ignore '^unexpected key "queue" for "concurrency" section' \
	  -ignore '^property "workflow_sha" is not defined in object type' \
	  -ignore '^specifying action "\$$/\.github/actions/(restore-private-ci|private-ci-inputs|service-release-notes)" in invalid format because ref is missing'

probe: ## actual accepted and rejected Go/SQL lint cases
	$(PYTHON) -B common/probe_lint.py

check: test policy-test lint probe
