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

## Delivery

- **Tracker:** GitHub `coghex/quruntul`, default branch `master`, through the
  Kanban plugin (`~/work/kanban`, never this project's tracker). The primary
  checkout is `~/work/quruntul`; it only fast-forwards to `master`. Implement
  in an isolated worktree under `.worktrees/` on a branch, and deliver by pull
  request.
- **One issue, one pull request, one worktree.** Code, tests, the
  documentation the change requires (including `docs/design.md`) and any
  evidence go together. Use `Closes #N` when the pull request completes the
  issue.
- **Mark every pull request's origin.** Its body must end with exactly one
  origin marker as its final line: `<!-- pr-origin:claude -->` or
  `<!-- pr-origin:codex -->`, naming the agent that wrote it. Put any
  attribution line above it. Review is routed to the other agent; a pull
  request without a valid marker counts as unknown origin and is reviewed by
  both. The `kanban:solve` workflow adds the marker; add it yourself when
  opening a pull request any other way.
- **Documentation** is tracked but never gated on CI or review. Land it on
  `master` with `docs-push [-m "message"] <paths...>` from any worktree as soon
  as it is ready, including documentation that goes with a code change: link
  it from the pull request instead of committing it to the pull request's
  branch. Markdown that tests read or agents execute is code and goes in the
  pull request; `docs-push` refuses it and says why.
- **Merging.** The PR drainer merges a pull request once it carries
  `reviewed:approve` and its `build-test` and `review-approved` checks pass.
  `build-test` requires every leg of the `test` matrix. A push that changes the
  pull request's own files removes the approval, so it needs a fresh review.
  Never merge or approve your own work, never merge on your own initiative,
  and never add review labels by hand.
- Never reset, clean or rebase another agent's worktree. Stop only processes
  you started.
