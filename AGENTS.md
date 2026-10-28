# FinAssist shared Go CI

This repository owns reusable Go CI, lint profiles and their regression tests. It is infrastructure, not a service template or a Go library. Consumers retain their own module identity, test suites and coverage policies.

## Workflow

- Work on a tooling branch from main; PRs target main and reference FIN-372.
- Commit messages and code/comments are English; PR title/body and review findings are Russian.
- Read README and docs/shared-go-ci.md before changing policy.
- Every test/process must have a real timeout. Never run UI tests or create tags.
- Preserve full-SHA pinning, grouped Renovate updates, matching bootstrap copies and rollback.
- Ordinary source PRs require owner authorization to merge. Do not change protections or secrets.
- Request both reviewers with an intentional comment beginning `@codex @claude`; include Security and Tests. After a pushed fix, reply/resolve findings and request both immediately without waiting for CI.

## Code Review Rules

### Security

Check PR/fork permissions, checkout credentials, secret gating to trusted pushes, and the read-only live-origin gate before write-enabled review. Preserve bounded Git operations, fixed source URL, raw cache integrity, fail-closed directory scans and local override restrictions. Treat a SHA as byte identity, not proof of authorship or ancestry main. Never execute PR code with write tokens.

### Tests

Map each changed behavior to an executable scenario. Cover valid profiles and invalid revisions/manifests, malicious cache state, local/pinned source parity, failure/skipped/cancelled gates, migration immutability, image provenance and service-specific smoke behavior. Use real local Git/filesystem fixtures where they prove the boundary. Test actual linter acceptance/rejection and reusable workflow syntax. Consumer full CI must preserve PG18/race and independent unit/integration/API coverage thresholds and the exact original library-template N/A exception.

### Compatibility

All 15 consumers and both service/library profiles share this source. Preserve caller repository identity, go.mod toolchain selection, tool versions, artifacts, required check names and documented overrides. Changes to the copied bootstrap require matching consumer copies in the same update PR.
