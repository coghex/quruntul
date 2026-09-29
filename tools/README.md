# Documentation landing helper

`docs_land.sh` and `docs_land_paths.py` are vendored from `coghex/kanban` at
commit `427b0ae19f5e6e3edab1b6456024db15d7a7e35f`, which is unchanged in both
files at `d6d1653270bc437c46c6beb45171aebb1a613f14`. Their upstream MIT
notice is retained in [KANBAN-LICENSE](KANBAN-LICENSE).

They are copied here from coghex/moskophoros, which carries Hetoimasia's local
adaptations: the suggested docs worktree path is `.worktrees/docs`, and a
regular, authoritative `AGENTS.md` lands under its own name. This repository
has no `docs/agent-workflow-contract.md`, so every validated document is
landable. Git publication, reconciliation, selected-path isolation and
reachability verification remain upstream behavior.

The scripts require Bash, Python 3, Git, `origin/master`, and registered
`master` and `docs-wip` worktrees. Create the docs worktree with:

```sh
git worktree add .worktrees/docs -b docs-wip origin/master
```

Use the installed `kanban:push-docs` skill after the owner requests
publication; inspect help, inventory and the dry run before landing:

```sh
tools/docs_land.sh -h
tools/docs_land.sh -l
tools/docs_land.sh -n -m "docs: describe the change" docs/example.md
```

This lane is for standalone documentation only. Documentation that accompanies
a code change belongs in that change's pull request.
