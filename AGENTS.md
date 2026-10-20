# Working agreements

Quruntul is a local testing lab (a standard-library Python engine) and the Codex
skills that drive it. [docs/design.md](docs/design.md) is the contract; change it
in the same commit as any behaviour it describes.

- The engine never touches a tracker, pushes, or merges. Only skills do that,
  under the consuming repository's own delivery rules.
- Evidence is immutable: never rewrite a finished trial, result or observation,
  and never replay a trial on recovery. Schema changes need an explicit
  migration and a test; never reset a ledger.
- Keep the engine dependency-free (Python 3.11+ standard library).
- A consuming repository's specifics belong in its `.quruntul/adapter.py`, not
  here. A consumer's legacy workflows belong to that consumer, in its own
  repository, never in quruntul.
- Run `python3 -m unittest discover -s tests -t .` before committing; tests use
  temporary repositories and never a real compiler, display or network.

## Delivery

The GitHub-project rules in the owner's global instructions apply. Specific to
this repository:

- **Tracker:** GitHub `coghex/quruntul`, default branch `master`; primary
  checkout `~/work/quruntul`.
- Documentation the change requires, including `docs/design.md`, lands with
  `docs-push` and is linked from the pull request body.
- **Merging:** the drainer needs `reviewed:approve`, `review-approved` and
  `build-test`, which requires every leg of the `test` matrix.
