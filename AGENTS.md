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
  here. Legacy Synarchy workflows stay verbatim under `skills/*/references/`
  until Synarchy has an adapter.
- Run `python3 -m unittest discover -s tests -t .` before committing; tests use
  temporary repositories and never a real compiler, display or network.
