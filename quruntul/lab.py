"""Lane orchestration: checkouts, recovery, flake batches and probe runs.

Selection is in select.py, durable records in state.py, process lifetime in
process.py. This module composes them and never interprets evidence beyond the
mechanical: which trial passed, which test failed, which status follows.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import platform
import random
import shutil
import subprocess
import time
import uuid

from quruntul import adapter as adapters
from quruntul import hspec, process, report, select
from quruntul.common import LabError, atomic_json, command, file_hash, git, utc
from quruntul.state import State

HARNESS = Path(__file__).resolve().parents[1]
STALE_RUN_SECONDS = 900
DEFAULT_TRIALS = 10
DEFAULT_REFRESH_DAYS = 7
# An ambient filter or RTS override must never silently change a trial.
SCRUBBED = ("GHCRTS",)


class Lab:
    def __init__(self, repo: str | Path = ".", log=print):
        self.root = Path(git(Path(repo), "rev-parse", "--show-toplevel"))
        self.common = Path(git(self.root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.directory = self.common / "quruntul"
        self.state = State(self.directory)
        self.log = log

    # -- revisions and checkouts --------------------------------------------

    def upstream(self, fetch: bool = True) -> tuple[str, str]:
        """The upstream default branch's ref and commit, fetched first."""
        try:
            ref = git(self.root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        except LabError:
            ref = next((r for r in ("origin/master", "origin/main")
                        if subprocess.run(["git", "rev-parse", "--verify", "--quiet", r], cwd=self.root,
                                          capture_output=True).returncode == 0), None)
            if ref is None:
                raise LabError("cannot find the upstream default branch (origin/HEAD, origin/master, origin/main)")
        if fetch:
            command(["git", "fetch", "--quiet", "origin", ref.split("/", 1)[1]], self.root, timeout=300)
        return ref, git(self.root, "rev-parse", "--verify", ref + "^{commit}")

    def resolve(self, ref: str | None) -> tuple[str, str, bool]:
        """(source ref, commit, whether it is the upstream head) for a run."""
        upstream_ref, upstream = self.upstream()
        if ref is None:
            return upstream_ref, upstream, True
        revision = git(self.root, "rev-parse", "--verify", ref + "^{commit}")
        return ref, revision, revision == upstream

    def checkout(self, revision: str) -> Path:
        """The shared detached worktree for one revision; never reset by the lab."""
        base = self.common.parent.parent / f".{self.common.parent.name}-quruntul"
        base.mkdir(exist_ok=True)
        target = base / revision
        with self._file_lock("checkouts.lock"):
            if not target.exists():
                command(["git", "worktree", "add", "--detach", str(target), revision], self.root, timeout=600)
        actual = Path(git(target, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        if actual != self.common or git(target, "rev-parse", "HEAD") != revision:
            raise LabError(f"unexpected checkout at {target}")
        if git(target, "branch", "--show-current"):
            raise LabError(f"lab checkout is no longer detached: {target}")
        if git(target, "status", "--porcelain", "--untracked-files=no"):
            raise LabError(f"lab checkout has edits; preserve and inspect {target}, it is never reset")
        return target

    @contextmanager
    def _file_lock(self, name: str):
        with (self.directory / name).open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def source(self, revision: str, ref: str) -> dict:
        subject, committed = git(self.root, "show", "-s", "--format=%s%n%cI", revision).split("\n", 1)
        return dict(ref=ref, subject=subject, committed=committed)

    def harness(self) -> dict:
        """Which quruntul produced the evidence. Uncommitted engine edits are recorded, not refused."""
        try:
            revision = git(HARNESS, "rev-parse", "HEAD")
            dirty = bool(git(HARNESS, "status", "--porcelain", "--", "quruntul"))
        except LabError:
            revision, dirty = None, True
        return dict(path=str(HARNESS), revision=revision, dirty=dirty)

    # -- recovery -----------------------------------------------------------

    def recover(self) -> list[str]:
        """Finish runs whose owner stopped heartbeating, from their retained files only."""
        recovered = []
        now = time.time()
        for run in self.state.runs(active=True, limit=1000):
            age = now - _epoch(run["heartbeat"])
            if age < STALE_RUN_SECONDS:
                continue
            directory = self.directory / "runs" / run["id"]
            for trial in self.state.trials(run["id"]):
                if trial["state"] == "running":
                    self.state.finish_trial(run["id"], trial["number"],
                                            dict(outcome="interrupted", reason="owner stopped before recording"), {})
            counts = Counter(t["state"] for t in self.state.trials(run["id"]))
            self.state.finish(run["id"], "interrupted", dict(
                reason=f"no heartbeat for {age:.0f}s; recorded trials kept, none replayed",
                counts=dict(counts), interpretation="inconclusive"))
            if directory.exists():
                atomic_json(directory / "result.json", self.state.run(run["id"]))
            recovered.append(run["id"])
        return recovered

    # -- building -----------------------------------------------------------

    def _runner(self, run_id: str, owner: str, artifacts: Path | None = None):
        artifacts = artifacts or self.directory / "runs" / run_id

        def run(argv, name, timeout, cwd, environment):
            env = _environment(environment)
            return process.run(argv, cwd, env, artifacts / name, timeout,
                               lambda: self.state.run_heartbeat(run_id, owner))
        return run

    def prepare(self, adapter, suite, checkout, revision, run_id, owner,
                artifacts: Path | None = None) -> adapters.Prepared:
        artifacts = artifacts or self.directory / "runs" / run_id
        self.adapter, self.adapter_ctx = adapter, adapters.Context(checkout, revision, artifacts,
                                                                   self._runner(run_id, owner, artifacts), self.log)
        """Build under the checkout's build claim; waits for another build, never races it."""
        resource = "build:" + revision
        waited = 0.0
        while self.state.claim(resource, owner, "build", dict(suite=suite.id)) is not None:
            if waited > 3600:
                raise LabError("waited an hour for another build in this checkout")
            self.state.run_heartbeat(run_id, owner)
            time.sleep(5)
            waited += 5
        try:
            ctx = adapters.Context(checkout, revision, artifacts, self._runner(run_id, owner, artifacts), self.log)
            try:
                prepared = adapter.prepare(ctx, suite)
            except LabError:
                raise
            except Exception as error:  # the adapter's own failure is a recorded blocker
                raise LabError(f"adapter could not prepare {suite.id}: {type(error).__name__}: {error}") from error
        finally:
            self.state.release(owner, resource)
        if not isinstance(prepared, adapters.Prepared):
            raise LabError("adapter.prepare must return quruntul.adapter.Prepared")
        return prepared

    def load(self, checkout: Path, revision: str):
        adapter = adapters.load(checkout)
        ctx = adapters.Context(checkout, revision, None, None, self.log)
        return adapter, adapters.suites(adapter, ctx)

    # -- flake --------------------------------------------------------------

    def flake(self, owner: str, target: str | None = None, ref: str | None = None,
              trials: int | None = None) -> dict:
        """One flake batch: the next suite with unmeasured tests, or an explicit target."""
        self.recover()
        source_ref, revision, upstream = self.resolve(ref)
        checkout = self.checkout(revision)
        adapter, suites = self.load(checkout, revision)
        for suite in suites:
            self.state.declare_suite(suite.record())
        by_id = {s.id: s for s in suites}
        claims = {c["resource"]: c for c in self.state.claims()}
        deferred = self.state.deferred()
        explicit_test = None
        if target:
            test = self.state.test(target)
            suite_id = test["suite"] if test else target
            if suite_id not in by_id:
                raise LabError(f"{target!r} is neither a suite nor a known test at {revision[:12]}")
            explicit_test = target if test else None
            order, skipped = [(by_id[suite_id], "explicit")], {}
            if suite_id in deferred:
                raise LabError(f"{suite_id} is deferred: {deferred[suite_id]['reason']}")
        else:
            tests = {}
            for row in self.state.tests():
                tests.setdefault(row["suite"], []).append(row)
            merged = self.merged_fixing()
            ledger = {s["id"]: s for s in self.state.suites()}
            order, skipped = select.flake_order(suites, ledger, tests, deferred, claims, platform.system(), owner, merged)
        trials = trials or int(adapters.option(adapter, "flake_trials", DEFAULT_TRIALS))
        for suite, why in order:
            outcome = self._flake_suite(adapter, suite, why, owner, checkout, revision, source_ref, upstream,
                                        trials, explicit_test, explicit=target is not None)
            if outcome["outcome"] != "nothing-new":
                outcome["skipped"] = skipped
                return outcome
            skipped[suite.id] = "enumerated; no new tests"
        return dict(outcome="no-candidate", revision=revision, skipped=skipped,
                    status=self.counts(), next_step="Every enumerated test has been measured; nothing new to flake.")

    def merged_fixing(self) -> set[str]:
        merged = set()
        for row in self.state.tests(status="fixing"):
            if row["pr"] and _pr_merged(self.root, row["pr"]):
                merged.add(row["id"])
        return merged

    def _flake_suite(self, adapter, suite, why, owner, checkout, revision, source_ref, upstream, trials,
                     explicit_test, explicit) -> dict:
        blocker = self.state.claim("suite:" + suite.id, owner, "flake", dict(revision=revision))
        if blocker:
            return dict(outcome="busy", suite=suite.id, holder=blocker["owner"])
        if suite.desktop and self.state.claim("desktop", owner, "flake", dict(suite=suite.id)):
            self.state.release(owner, "suite:" + suite.id)
            return dict(outcome="busy", suite=suite.id, reason="desktop in use")
        run_id = self.state.begin("flake", suite.id, revision, source_ref, upstream, dict(
            suite=suite.record(), reason=why, trials=trials, harness=self.harness(), owner=owner,
            platform=platform.system(), explicit_test=explicit_test))
        artifacts = self.directory / "runs" / run_id
        artifacts.mkdir(parents=True, exist_ok=True)
        self.log(f"flake {suite.id} ({why}) at {revision[:12]}; evidence: {artifacts}")
        state_name, detail, selected, measured = "blocked", None, [], {}
        try:
            prepared = self.prepare(adapter, suite, checkout, revision, run_id, owner)
            paths = self._enumerate(suite, prepared, run_id, owner, artifacts)
            change = self.state.enumerated(suite.record(), suite.identity, revision, paths, upstream)
            self._seed(suite, change["added"])
            rows = {r["id"]: r for r in self.state.tests(suite=suite.id)}
            merged = self.merged_fixing()
            if explicit_test:
                selected = [explicit_test]
            else:
                order = {f"{suite.id}::{p}": n for n, p in enumerate(paths)}
                verify = [i for i, r in rows.items() if r["status"] == "fixing" and i in merged]
                fresh = sorted((i for i, r in rows.items() if r["status"] == "new"), key=lambda i: order.get(i, 1 << 30))
                if suite.batch_tests:
                    fresh = fresh[:max(0, suite.batch_tests - len(verify))]
                selected = verify + fresh
                if explicit and not selected:
                    selected = [i for i, r in rows.items() if r["status"] != "retired"]
            self.state.update_run(run_id, self.state.run(run_id)["document"] | dict(
                prepared=dict(argv=prepared.argv, cwd=prepared.cwd, provenance=prepared.provenance,
                              wrapper=prepared.wrapper),
                enumerated=len(paths), added=len(change["added"]), selected=selected))
            if not selected:
                state_name = "nothing-new"
                self.state.finish(run_id, "nothing-new", dict(enumerated=len(paths), interpretation="clean"))
                return dict(outcome="nothing-new", suite=suite.id)
            whole = len(selected) == len([r for r in rows.values() if r["status"] != "retired"])
            state_name, detail = self._trials(suite, prepared, run_id, owner, artifacts, selected, trials, whole)
            measured = self._decide(suite, run_id, revision, selected, upstream, explicit, trials)
        except LabError as error:
            state_name, detail = "blocked", str(error)
        except KeyboardInterrupt:
            state_name, detail = "interrupted", "user interrupted; owned processes stopped"
        finally:
            for trial in self.state.trials(run_id):
                if trial["state"] == "running":
                    self.state.finish_trial(run_id, trial["number"], dict(outcome="interrupted"), {})
            counts = dict(Counter(t["state"] for t in self.state.trials(run_id)))
            newly_flaky = [t for t, m in measured.items() if m.get("became") == "flaky"]
            newly_failing = [t for t, m in measured.items() if m.get("became") == "failing"]
            summary = dict(counts=counts, reason=detail, planned_trials=trials, selected=len(selected),
                           measured=measured, newly_flaky=newly_flaky, newly_failing=newly_failing,
                           interpretation=("blocked" if state_name == "blocked" else
                                           "observations" if newly_flaky or any(m["failures"] or m.get("missing")
                                                                                for m in measured.values()) else
                                           "clean" if state_name == "complete" else "inconclusive"))
            if state_name != "nothing-new":
                self.state.finish(run_id, state_name, summary)
                self._flake_report(run_id, suite, summary, detail)
            self.state.release(owner, "suite:" + suite.id)
            if suite.desktop:
                self.state.release(owner, "desktop")
        run = self.state.run(run_id)
        return dict(outcome=state_name, run_id=run_id, suite=suite.id, revision=revision,
                    candidate=not upstream, summary=run["summary"],
                    result=str(artifacts / "result.json"), report=str(artifacts / "report.md"),
                    ledger=str(self.render()))

    def _enumerate(self, suite, prepared, run_id, owner, artifacts) -> list[str]:
        if suite.framework in ("command", "exit"):
            return list(suite.checks)
        argv = prepared.wrapper + _launch(prepared, hspec.enumerate_argv(prepared.argv[0], prepared.argv[1:]))
        result = process.run(argv, Path(prepared.cwd), _environment(prepared.environment), artifacts / "enumerate",
                             600, lambda: self.state.run_heartbeat(run_id, owner))
        if result["outcome"] != "passed":
            raise LabError(f"enumeration {result['outcome']}; see {result['log']}")
        paths = hspec.enumerate_examples(Path(result["log"]).read_text(errors="replace"))
        if not paths:
            raise LabError(f"enumeration found no examples; see {result['log']}")
        return paths

    def _trials(self, suite, prepared, run_id, owner, artifacts, selected, trials, whole):
        deadline = time.monotonic() + suite.batch_seconds
        paths = [self.state.test(t)["path"] for t in selected]
        state_name, detail = "complete", None
        for number in range(1, trials + 1):
            if deadline - time.monotonic() < suite.trial_seconds:
                return "budget-exhausted", f"batch budget left room for {number - 1} of {trials} trials"
            result, failure_report, trial = self._run_trial(suite, prepared, run_id, owner, artifacts, number,
                                                            None if whole else paths)
            outcomes = self._outcomes(suite, result, selected, failure_report, Path(trial["prefix"] + ".checks.json"),
                                      trial)
            self.state.finish_trial(run_id, number, result, outcomes)
            failed = sum(1 for o in outcomes.values() if o == "failed")
            self.log(f"{suite.id} {number}/{trials}: {result['outcome']}, {failed} failed of {len(selected)} "
                     f"({result['duration_seconds']:.1f}s)")
            if result["outcome"] in ("interrupted", "harness-error", "setup-error"):
                return ("interrupted" if result["outcome"] == "interrupted" else "blocked"), result["outcome"]
        return state_name, detail

    def _run_trial(self, suite, prepared, run_id, owner, artifacts, number, paths, record=None):
        """Launch one trial as every lane does: the prepared argv, working directory, environment and
        wrapper, the trial_env hook, and for Hspec exact selection of `paths` (None: the whole suite).

        `number` is the suite's own trial number, which the hooks and the process see; `record` numbers
        the run's trial row when one run holds several suites."""
        name = f"trial-{number:04}"
        seed = random.SystemRandom().randrange(1, 2 ** 31)
        trial = dict(number=number, prefix=str(artifacts / name), seed=seed)
        env = dict(prepared.environment, QURUNTUL_TRIAL=str(number), QURUNTUL_SEED=str(seed),
                   QURUNTUL_PROBE_RESULT=str(artifacts / f"{name}.checks.json"),
                   QURUNTUL_TRIAL_PREFIX=str(artifacts / name))
        env.update(self._hook("trial_env", suite, trial) or {})
        if suite.framework == "hspec":
            failure_report = artifacts / f"{name}.failures"
            argv = prepared.wrapper + _launch(prepared, hspec.trial_argv(
                prepared.argv[0], paths, seed, str(failure_report), suite.rts, prepared.argv[1:]))
        else:
            failure_report = None
            argv = prepared.wrapper + prepared.argv
        document = dict(argv=argv, seed=seed, started=utc())
        if record is not None:
            document["suite"] = suite.id
        self.state.start_trial(run_id, record or number, document)
        result = process.run(argv, Path(prepared.cwd), _environment(env), artifacts / name, suite.trial_seconds,
                             lambda: self.state.run_heartbeat(run_id, owner))
        result["seed"] = seed
        return result, failure_report, trial

    def _hook(self, name, suite, trial):
        """An optional adapter hook; its failure is recorded against the trial, never silently ignored."""
        adapter = getattr(self, "adapter", None)
        function = getattr(adapter, name, None) if adapter is not None else None
        if function is None:
            return None
        try:
            return function(self.adapter_ctx, suite, trial)
        except Exception as error:
            raise LabError(f"adapter {name} failed for {suite.id}: {type(error).__name__}: {error}") from error

    def _outcomes(self, suite, result, selected, failure_report, checks_path, trial=None) -> dict[str, str]:
        """Each selected test's outcome in one trial. Absence after a crash is 'incomplete', never 'passed'."""
        by_path = {self.state.test(t)["path"]: t for t in selected}
        outcomes = {}
        if suite.framework == "hspec":
            known = {t["path"] for t in self.state.tests(suite=suite.id)}
            reported = hspec.parse_checks(Path(result["log"]).read_text(errors="replace"), known)
            failed = set()
            if failure_report and failure_report.exists():
                try:
                    failed = set(hspec.parse_failure_report(failure_report.read_text(errors="replace")))
                except (ValueError, IndexError):
                    result["failure_report_error"] = "unreadable failure report"
            for path, test_id in by_path.items():
                seen = reported.get(path)
                if path in failed:
                    outcomes[test_id] = "failed"
                elif seen:
                    outcomes[test_id] = seen
                else:
                    outcomes[test_id] = ("missing" if result["outcome"] in ("passed", "failed") else "incomplete")
            return outcomes
        if suite.framework == "exit":
            outcome = {"passed": "passed", "failed": "failed", "crashed": "failed", "timeout": "failed"}.get(
                result["outcome"], "incomplete")
            return {test_id: outcome for test_id in selected}
        if getattr(getattr(self, "adapter", None), "outcomes", None) is not None:
            checks = self._hook("outcomes", suite, dict(trial or {}, result=result))
            if checks is None:
                if result["outcome"] in ("passed", "failed"):
                    result["outcome"], result["error"] = "harness-error", "the adapter could not read this trial's checks"
                return {t: "incomplete" for t in selected}
            unknown = set(checks) - set(suite.checks)
            if unknown or any(v not in ("passed", "failed", "unproven", "missing") for v in checks.values()):
                result["outcome"], result["error"] = "harness-error", f"adapter reported unknown checks {sorted(unknown)}"
                return {t: "incomplete" for t in selected}
            mapped = {"passed": "passed", "failed": "failed", "missing": "failed", "unproven": "incomplete"}
            return {test_id: mapped[checks[path]] if path in checks else "incomplete"
                    for path, test_id in by_path.items()}
        try:
            document = json.loads(Path(checks_path).read_text())
            checks = document["checks"]
            if document.get("schema") != "quruntul-probe/v1" or set(checks) != set(suite.checks):
                raise ValueError("the probe must report exactly its declared checks")
            if any(v not in ("passed", "failed", "unproven") for v in checks.values()):
                raise ValueError("unknown check outcome")
            if (result["returncode"] == 0) != all(v == "passed" for v in checks.values()):
                raise ValueError("exit status disagrees with checks")
        except (OSError, ValueError, KeyError, TypeError) as error:
            if result["outcome"] in ("passed", "failed"):
                result["outcome"], result["error"] = "harness-error", f"invalid probe report: {error}"
            return {t: "incomplete" for t in selected}
        return {test_id: checks[path] if checks[path] != "unproven" else "incomplete" for path, test_id in by_path.items()}

    def _seed(self, suite, added: list[str]) -> None:
        """Carry an older lab's verdicts into tests the ledger has only just met.

        Only a test that is still `new` is seeded, and only to stable or flaky,
        each with the adapter's reason and evidence recorded as a status event.
        """
        if not added:
            return
        seeds = self._hook("seed", suite, dict(added=[self.state.test(t)["path"] for t in added])) or {}
        for test_id in added:
            row = self.state.test(test_id)
            seed = seeds.get(row["path"])
            if not seed or row["status"] != "new" or seed.get("status") not in ("stable", "flaky"):
                continue
            self.state.set_status(test_id, seed["status"], "seeded: " + str(seed.get("reason", "")),
                                  dict(seed=seed.get("evidence", {})))

    def _decide(self, suite, run_id, revision, selected, upstream, explicit, requested) -> dict:
        """Counters for every selected test; status changes only for an upstream-head batch."""
        results = self.state.results(run_id)
        trials = {t["number"]: t for t in self.state.trials(run_id)}
        measured = {}
        for test_id in selected:
            mine = [r for r in results if r["test_id"] == test_id]
            passes = sum(1 for r in mine if r["outcome"] == "passed")
            failures = sum(1 for r in mine if r["outcome"] == "failed")
            complete = sum(1 for r in mine if r["outcome"] in ("passed", "failed", "pending"))
            missing = sum(1 for r in mine if r["outcome"] == "missing")
            outcome = "failed" if failures else ("passed" if complete and passes == complete else "incomplete")
            self.state.record_measurement(test_id, run_id, revision, complete, failures, outcome)
            row = self.state.test(test_id)
            became = None
            # Every requested trial ran and completed as a process. A batch cut
            # short (its budget, a harness error) has fewer trials than it asked
            # for, and proves neither stability nor a consistent failure.
            planned = requested
            whole = len(trials) == requested and all(t["state"] in ("passed", "failed") for t in trials.values())
            # Failed in every trial: a consistent failure, not flakiness. In a
            # young project the harness or the environment is the first
            # suspect, so it goes to assessment rather than $deflake. This is
            # the batch's evidence, whatever the test's status and whether
            # the status changes (a failing test measured again, a candidate).
            consistent = bool(failures and failures == planned > 1 and whole)
            if upstream:
                # A failing test measured again is judged afresh, as a new one
                # is: all trials failing keeps it failing, some makes it
                # flaky ($deflake's), none makes it stable.
                if consistent and row["status"] in ("new", "stable", "fixing"):
                    became = "failing"
                elif failures and not consistent and row["status"] in ("new", "stable", "fixing", "failing"):
                    became = "flaky"
                elif (not failures and row["status"] in ("new", "fixing", "failing") and passes == planned
                      and whole):
                    became = "stable"
                elif (not failures and not passes and row["status"] == "new" and complete == planned
                      and whole):
                    # Pending in every trial: this environment never exercises it.
                    # It is not measured, so it is neither stable nor new work.
                    became = "pending"
                if became:
                    self.state.set_status(test_id, became, f"flake batch {run_id}: {failures} failed of {complete}",
                                          dict(run=run_id, revision=revision))
            measured[test_id] = dict(passes=passes, failures=failures, complete=complete, became=became,
                                     missing=missing, consistent=consistent,
                                     failing_trials=[r["number"] for r in mine if r["outcome"] == "failed"])
        return measured

    def _flake_report(self, run_id, suite, summary, detail):
        run = self.state.run(run_id)
        artifacts = self.directory / "runs" / run_id
        observations = []
        failing = sorted(t for t, m in summary["measured"].items() if m.get("consistent"))
        if failing:
            # One observation for the lot: tests that fail every trial of one
            # batch usually share one cause.
            logs = ", ".join(f"`runs/{run_id}/trial-{n:04}.log`"
                             for n in summary["measured"][failing[0]]["failing_trials"][:5])
            observations.append(dict(
                title=f"{len(failing)} tests of {suite.id} failed every trial",
                area=suite.area or suite.id, kind="uncertain",
                tests=", ".join(failing[:5]) + (f" and {len(failing) - 5} more" if len(failing) > 5 else ""),
                evidence=f"{logs}; run {run_id}",
                expected="every trial passes",
                observed=f"each failed all {summary['planned_trials']} trials; statuses now: " + ", ".join(
                    f"{status} {count}" for status, count in sorted(Counter(
                        self.state.test(t)["status"] for t in failing).items())),
                confidence="medium",
                follow_up="$assess-tests: a consistent failure is not flakiness; check the harness and "
                          "environment before the product"))
        for test_id, m in sorted(summary["measured"].items()):
            if not m["failures"] or m.get("consistent"):
                continue
            logs = ", ".join(f"`runs/{run_id}/trial-{n:04}.log`" for n in m["failing_trials"][:5])
            observations.append(dict(
                title=f"{test_id} failed {m['failures']} of {m['complete']} trials",
                area=suite.area or suite.id, kind="flaky-test", tests=test_id,
                evidence=f"{logs}; seeds in the trial results; run {run_id}",
                expected="every trial passes",
                observed=f"{m['failures']} failures in {m['complete']} complete trials" +
                         (f"; status now {m['became']}" if m["became"] else ""),
                confidence="high", follow_up="$deflake " + test_id))
        unreported = sorted(t for t, m in summary["measured"].items() if m.get("missing"))
        if unreported:
            # A trial that ran to completion yet named no result for a selected
            # test: the log could not be read for it, so it stays unmeasured.
            observations.append(dict(
                title=f"{len(unreported)} selected tests of {suite.id} went unreported by a completed trial",
                area=suite.area or suite.id, kind="harness", tests=", ".join(unreported[:5]) +
                (f" and {len(unreported) - 5} more" if len(unreported) > 5 else ""),
                evidence=f"`runs/{run_id}/trial-*.log`; outcome `missing` in the run's results",
                expected="every completed trial reports every selected test",
                observed=f"{len(unreported)} tests reported in no completed trial; those left unmeasured",
                confidence="high", follow_up="inspect the trial log's Hspec output against the enumeration"))
        if summary["interpretation"] == "blocked":
            observations.append(dict(
                title=f"flake batch of {suite.id} blocked", area=suite.area or suite.id, kind="harness",
                tests="none", evidence=f"`runs/{run_id}/` logs; {detail}", expected="the suite builds and runs",
                observed=str(detail), confidence="medium", follow_up="inspect the build/enumeration log"))
        facts = "\n".join([
            f"- Suite: `{suite.id}` ({suite.kind}, {suite.framework}); selected because: {run['document']['reason']}",
            f"- Trials: {summary['counts']} of {summary['planned_trials']} planned; selected tests: {summary['selected']}",
            f"- Became stable: {sum(1 for m in summary['measured'].values() if m['became'] == 'stable')}; "
            f"became flaky: {len(summary['newly_flaky'])}; became failing: {len(summary['newly_failing'])}",
            f"- Result: `runs/{run_id}/result.json`; raw logs beside it",
        ])
        status = summary["interpretation"] if summary["interpretation"] != "clean" or not observations else "observations"
        if status == "inconclusive" and not observations:
            limitations = f"The batch ended as {run['state']}: {detail}. No test changed status on incomplete evidence."
        else:
            limitations = ("A clean batch is evidence of stability over these trials, not proof. "
                           "Seeds varied per trial; test order was the suite's own.")
        text = report.skeleton(run, self.source(run["revision"], run["source_ref"]), facts, status,
                               interpretation="Mechanical flake measurement; failures are listed as observations "
                                              "for $assess-tests and $deflake.",
                               limitations=limitations, observations=observations)
        report.write(artifacts / "report.md", text)
        if observations:
            self.state.add_observations(run_id, report.parse(text)["observations"])
        atomic_json(artifacts / "result.json", self.state.run(run_id))

    # -- shakedown -------------------------------------------------------------

    def shakedown(self, owner: str, target: str | None = None) -> dict:
        """One trial of every suite that applies here, or of one target suite, at the upstream head.

        Each suite is launched exactly as a flake batch of the whole suite is, and every problem it
        shows is recorded. Nothing about tests or suites is written to the ledger: only this run, its
        trials and results, its report and its observations.
        """
        source_ref, revision, _ = self.resolve(None)
        checkout = self.checkout(revision)
        adapter, suites = self.load(checkout, revision)
        deferred = self.state.deferred()
        if target:
            suites = [s for s in suites if s.id == target]
            if not suites:
                raise LabError(f"{target!r} is not a suite at {revision[:12]}; a shakedown runs whole suites")
            if target in deferred:
                raise LabError(f"{target} is deferred: {deferred[target]['reason']}")
        here = platform.system()
        run_id = self.state.begin("shakedown", target, revision, source_ref, True, dict(
            reason="explicit" if target else "every suite", suites=[s.id for s in suites], harness=self.harness(),
            owner=owner, platform=here))
        artifacts = self.directory / "runs" / run_id
        artifacts.mkdir(parents=True, exist_ok=True)
        self.log(f"shakedown of {target or f'{len(suites)} suites'} at {revision[:12]}; evidence: {artifacts}")
        entries: dict[str, dict] = {}
        state_name, detail, number, done = "complete", None, 0, False
        try:
            for suite in suites:
                if state_name != "complete":
                    break
                if suite.id in deferred:
                    entries[suite.id] = dict(result="skipped", reason="deferred: " + deferred[suite.id]["reason"])
                    continue
                if here not in suite.platforms:
                    entries[suite.id] = dict(result="skipped", reason="platform-inapplicable: runs on "
                                                                      + ", ".join(suite.platforms))
                    continue
                holder = self.state.claim("suite:" + suite.id, owner, "shakedown", dict(revision=revision))
                if holder:
                    entries[suite.id] = dict(result="busy", reason=f"claimed by {holder['owner']} ({holder['lane']})")
                    continue
                if suite.desktop:
                    holder = self.state.claim("desktop", owner, "shakedown", dict(suite=suite.id))
                    if holder:
                        self.state.release(owner, "suite:" + suite.id)
                        entries[suite.id] = dict(result="busy", reason=f"desktop in use by {holder['owner']} "
                                                                       f"({holder['lane']})")
                        continue
                number += 1
                try:
                    entries[suite.id], interrupted = self._shake_suite(adapter, suite, owner, checkout, revision,
                                                                       run_id, number, artifacts / suite.id)
                finally:
                    self.state.release(owner, "suite:" + suite.id)
                    if suite.desktop:
                        self.state.release(owner, "desktop")
                if interrupted:
                    state_name, detail = "interrupted", f"user interrupted during {suite.id}; owned processes stopped"
            done = True
        except KeyboardInterrupt:
            state_name, detail = "interrupted", "user interrupted between suites; owned processes stopped"
        except LabError as error:
            state_name, detail = "blocked", str(error)
        finally:
            if not done and state_name == "complete":
                state_name, detail = "blocked", "stopped by an unexpected error"
            for trial in self.state.trials(run_id):
                if trial["state"] == "running":
                    self.state.finish_trial(run_id, trial["number"], dict(outcome="interrupted"), {})
            for suite in suites:
                entries.setdefault(suite.id, dict(result="not-run", reason=f"the shakedown was {state_name} first"))
            entries = {s.id: entries[s.id] for s in suites}
            results = Counter(e["result"] for e in entries.values())
            problems = sorted(i for i, e in entries.items() if e.get("problems"))
            interpretation = ("observations" if problems else
                              "clean" if state_name == "complete" and number and set(results) <= {"clean", "skipped"}
                              else "inconclusive")
            summary = dict(suites=entries, results=dict(results), reason=detail, interpretation=interpretation,
                           counts=dict(Counter(t["state"] for t in self.state.trials(run_id))))
            self.state.finish(run_id, state_name, summary)
            self._shake_report(run_id, {s.id: s for s in suites}, summary)
        return dict(outcome=state_name, run_id=run_id, revision=revision, target=target, reason=detail,
                    interpretation=interpretation, results=summary["results"], suites=entries,
                    result=str(artifacts / "result.json"), report=str(artifacts / "report.md"))

    def _shake_suite(self, adapter, suite, owner, checkout, revision, run_id, number, directory):
        """One suite's build, enumeration and trial, and every problem they show.

        Returns the suite's entry and whether the shakedown was interrupted during it. The trial is the
        suite's first (hooks and the process see trial 1); the run records it as trial `number`.
        """
        directory.mkdir(parents=True, exist_ok=True)
        self.log(f"shakedown {suite.id}; evidence: {directory}")
        entry = dict(result="clean", problems=[], artifacts=str(directory))
        problems, tests, stage, interrupted = entry["problems"], {}, "build", False
        prefix = directory / "trial-0001"
        try:
            try:
                prepared = self.prepare(adapter, suite, checkout, revision, run_id, owner, directory)
            except LabError as error:
                problems.append(dict(kind="build-failed", reason=str(error), logs=_logs(directory), tests=[]))
                return _headline(entry), False
            entry["prepared"] = dict(argv=prepared.argv, cwd=prepared.cwd, provenance=prepared.provenance,
                                     wrapper=prepared.wrapper, launches_executable=prepared.launches_executable)
            stage = "enumeration"
            try:
                paths = self._enumerate(suite, prepared, run_id, owner, directory)
            except LabError as error:
                log = directory / "enumerate.log"
                problems.append(dict(kind="enumeration-failed", reason=str(error),
                                     log=str(log) if log.exists() else None, tests=[]))
                return _headline(entry), False
            # The current enumeration names the tests, whatever the ledger holds.
            tests = {f"{suite.id}::{p}": p for p in paths}
            entry.update(listed=len(tests), ledger=self._drift(suite, tests))
            stage = "trial"
            try:
                result, failure_report, trial = self._run_trial(suite, prepared, run_id, owner, directory, 1, None,
                                                                record=number)
                outcomes = self._shake_outcomes(suite, result, tests, failure_report, trial)
            except (LabError, RuntimeError, OSError) as error:
                result = dict(outcome="harness-error", error=f"{type(error).__name__}: {error}",
                              log=str(prefix) + ".log")
                outcomes = {t: "incomplete" for t in tests}
            self._record_trial(run_id, number, suite, result, outcomes)
        except KeyboardInterrupt:
            interrupted = True
            if stage != "trial":
                problems.append(dict(kind="incomplete", outcome="interrupted",
                                     detail=f"the shakedown was interrupted during the {stage}", log=None,
                                     tests=list(tests)))
                return _headline(entry), True
            row = next((t for t in self.state.trials(run_id) if t["number"] == number), None)
            if row is None or row["state"] == "running":
                # Keep what the stopped trial left: its log, and any failure it had already reported.
                result = _retained(prefix)
                result["seed"] = (row or {}).get("document", {}).get("seed")
                try:
                    outcomes = self._shake_outcomes(
                        suite, result, tests, Path(str(prefix) + ".failures") if suite.framework == "hspec" else None,
                        dict(number=1, prefix=str(prefix), seed=result["seed"]))
                except Exception:  # the evidence stays on disk; nothing more is readable
                    outcomes = {t: "incomplete" for t in tests}
                self._record_trial(run_id, number, suite, result, outcomes)
        row = next(t for t in self.state.trials(run_id) if t["number"] == number)
        recorded = {r["test_id"]: r["outcome"] for r in self.state.results(run_id) if r["number"] == number}
        _classify(entry, row["document"], number, tests, recorded)
        return _headline(entry), interrupted

    def _record_trial(self, run_id, number, suite, result, outcomes):
        if not any(t["number"] == number for t in self.state.trials(run_id)):
            # A hook failed or an interruption came before the process started; the trial is still recorded.
            self.state.start_trial(run_id, number, dict(suite=suite.id, started=utc()))
        result["suite"] = suite.id
        self.state.finish_trial(run_id, number, result, outcomes)

    def _drift(self, suite, tests: dict[str, str]) -> dict:
        """How the enumeration differs from the ledger. Reported, never recorded."""
        live = {r["id"] for r in self.state.tests(suite=suite.id) if r["status"] != "retired"}
        return dict(unrecorded=[t for t in tests if t not in live], unlisted=sorted(live - set(tests)))

    def _shake_outcomes(self, suite, result, tests, failure_report, trial) -> dict[str, str]:
        """Each listed test's own result in a shakedown trial, read against the current enumeration.

        Native results are kept: passed, failed, pending (Hspec) and unproven (probes). A test with no
        result is `missing` when the trial completed and `incomplete` when it did not. Unreadable or
        malformed protocol evidence makes a completed trial a harness error, keeping any failure that
        can still be read on its own.
        """
        def broken(reason):
            if result["outcome"] in ("passed", "failed"):
                result["outcome"], result["error"] = "harness-error", reason

        by_path = {path: test_id for test_id, path in tests.items()}
        if suite.framework == "hspec":
            log = Path(result.get("log") or str(trial["prefix"]) + ".log")
            found = hspec.parse_checks(log.read_text(errors="replace"), set(by_path)) if log.exists() else {}
            if failure_report and failure_report.exists():
                try:
                    failed = hspec.parse_failure_report(failure_report.read_text(errors="replace"))
                except (ValueError, IndexError):
                    result["failure_report_error"] = "unreadable failure report"
                    broken("unreadable failure report")
                else:
                    found.update({p: "failed" for p in failed if p in by_path})
        elif suite.framework == "exit":
            found = {"run": result["outcome"]} if result["outcome"] in ("passed", "failed") else {}
        else:
            found = self._probe_checks(suite, result, trial, broken)
        absent = "missing" if result["outcome"] in ("passed", "failed") else "incomplete"
        return {test_id: found[path] if found.get(path, "missing") != "missing" else absent
                for path, test_id in by_path.items()}

    def _probe_checks(self, suite, result, trial, broken) -> dict[str, str]:
        """A command suite's checks from the adapter's `outcomes` hook or its quruntul-probe/v1 report."""
        declared = set(suite.checks)
        hooked = getattr(getattr(self, "adapter", None), "outcomes", None) is not None
        if hooked:
            valid = ("passed", "failed", "unproven", "missing")
            try:
                checks = self._hook("outcomes", suite, dict(trial, result=result))
            except LabError as error:
                broken(str(error))
                return {}
            if checks is None:
                broken("the adapter could not read this trial's checks")
                return {}
        else:
            valid = ("passed", "failed", "unproven")
            try:
                document = json.loads(Path(trial["prefix"] + ".checks.json").read_text())
                if document.get("schema") != "quruntul-probe/v1":
                    raise ValueError("not a quruntul-probe/v1 report")
                checks = document["checks"]
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                broken(f"invalid probe report: {error}")
                return {}
        if not isinstance(checks, dict):
            broken("the checks are not a mapping")
            return {}
        readable = {c: v for c, v in checks.items() if c in declared and v in valid}
        unknown = sorted(set(checks) - declared)
        invalid = sorted(c for c in checks if c in declared and c not in readable)
        if unknown or invalid:
            broken(f"unknown checks {unknown}, unknown outcomes for {invalid}")
            return {c: v for c, v in readable.items() if v == "failed"}
        # As in a flake batch, a probe's exit status must agree with the checks it reported.
        if (not hooked and result["outcome"] in ("passed", "failed")
                and (result.get("returncode") == 0) != all(v == "passed" for v in readable.values())):
            broken("exit status disagrees with checks")
            return {c: v for c, v in readable.items() if v == "failed"}
        return readable

    def _shake_report(self, run_id, suites, summary):
        run = self.state.run(run_id)
        artifacts = self.directory / "runs" / run_id

        def where(path):
            if not path:
                return None
            try:
                return f"`{Path(path).relative_to(self.directory)}`"
            except ValueError:
                return f"`{path}`"

        def ids(tests):
            return ", ".join(f"`{t}`" for t in tests)

        lines, observations, pending = [], [], []
        for suite_id, entry in summary["suites"].items():
            if "problems" not in entry:
                lines.append(f"- `{suite_id}`: {entry['result']} ({entry['reason']})")
                continue
            trial = entry.get("trial") or {}
            lines.append(f"- `{suite_id}`: {entry['result']}" + (
                f"; {entry['listed']} listed; its trial (the run's trial {trial['number']}) {trial['outcome']}, "
                f"log {where(trial['log'])}"
                if trial else ""))
            said = []
            for problem in entry["problems"]:
                text = _problem_text(problem)
                said.append(text)
                lines.append(f"  - {problem['kind']}: {text}" + (
                    f"; tests ({len(problem['tests'])}): {ids(problem['tests'])}" if problem["tests"] else ""))
            for kind, tests in entry.get("not_passed", {}).items():
                pending.append(f"{len(tests)} {kind} in `{suite_id}`")
                lines.append(f"  - reported {kind}, not passed ({len(tests)}): {ids(tests)}")
            drift = entry.get("ledger") or {}
            if drift.get("unrecorded"):
                lines.append(f"  - not in the ledger, and not recorded ({len(drift['unrecorded'])}): "
                             f"{ids(drift['unrecorded'])}")
            if drift.get("unlisted"):
                lines.append(f"  - in the ledger but no longer listed, and not retired ({len(drift['unlisted'])}): "
                             f"{ids(drift['unlisted'])}")
            if not entry["problems"]:
                continue
            suite = suites[suite_id]
            affected = list(dict.fromkeys(t for p in entry["problems"] for t in p["tests"]))
            evidence = [where(p) for problem in entry["problems"]
                        for p in [problem.get("log"), problem.get("failure_report"), *problem.get("logs", [])] if p]
            observations.append(dict(
                title=f"shakedown of {suite_id}: {entry['result']}" + (
                    f" (also {', '.join(p['kind'] for p in entry['problems'][1:])})" if len(entry["problems"]) > 1
                    else ""),
                area=suite.area or suite.id, kind="harness", tests=", ".join(affected) or "none",
                evidence="; ".join(list(dict.fromkeys(evidence)) + [f"run {run_id}"]),
                expected="the suite builds, lists its tests, completes one trial, and every listed test "
                         "reports a result with none failed",
                observed="; ".join(f"{p['kind']}: {t}" for p, t in zip(entry["problems"], said)),
                confidence="high",
                follow_up="$assess-tests: suspect the harness and environment first (the adapter's build and "
                          "launch against how CI runs the suite), then the product"))
        results = ", ".join(f"{k} {v}" for k, v in sorted(summary["results"].items()))
        facts = "\n".join([
            f"- Shakedown of {len(summary['suites'])} suites at the upstream head: one trial each, launched as a "
            f"flake batch of the whole suite; results: {results}; state {run['state']}"
            + (f" ({summary['reason']})" if summary["reason"] else ""),
            f"- Result: `runs/{run_id}/result.json`; each suite's evidence under `runs/{run_id}/<suite>/`",
            *lines])
        limitations = ("One trial per suite proves the launch, not stability; flake batches measure that. "
                       "Nothing about tests or suites was recorded in the ledger, and flake and $test selection "
                       "never read this result.")
        if pending:
            limitations += " Reported but not passed, and so unproven here: " + "; ".join(pending) + "."
        if summary["interpretation"] == "inconclusive":
            limitations += " Not every applicable suite ran; see the results above."
        text = report.skeleton(run, self.source(run["revision"], run["source_ref"]), facts, summary["interpretation"],
                               interpretation="Mechanical shakedown: each suite with a problem is one observation "
                                              "for $assess-tests, covering all of its problems. Advisory only.",
                               limitations=limitations, observations=observations)
        report.write(artifacts / "report.md", text)
        if observations:
            self.state.add_observations(run_id, report.parse(text)["observations"])
        atomic_json(artifacts / "result.json", self.state.run(run_id))

    # -- test (probes) --------------------------------------------------------

    def test(self, owner: str, target: str | None = None, hint: str | None = None, ref: str | None = None,
             desktop: bool = False) -> dict:
        """One observational execution of one probe; the agent then writes its report."""
        self.recover()
        source_ref, revision, upstream = self.resolve(ref)
        checkout = self.checkout(revision)
        adapter, suites = self.load(checkout, revision)
        for suite in suites:
            self.state.declare_suite(suite.record())
        claims = {c["resource"]: c for c in self.state.claims()}
        deferred = self.state.deferred()
        refresh = int(adapters.option(adapter, "refresh_days", DEFAULT_REFRESH_DAYS))
        if target:
            match = [s for s in suites if s.id == target]
            if not match:
                raise LabError(f"{target!r} is not a suite at {revision[:12]}")
            if match[0].kind != "probe":
                raise LabError(f"{target} is a CI suite; $test runs probes only (use $flake for CI suites)")
            if target in deferred:
                raise LabError(f"{target} is deferred: {deferred[target]['reason']}")
            if match[0].desktop and not desktop:
                raise LabError(f"{target} opens windows; rerun with --desktop after the owner approves this session")
            order, skipped = [(match[0], "explicit")], {}
        else:
            ledger = {s["id"]: s for s in self.state.suites()}
            order, skipped = select.test_order(suites, ledger, deferred, claims, platform.system(), owner, refresh,
                                               hint=hint)
        if not order:
            return dict(outcome="no-candidate", skipped=skipped,
                        pending_proposals=self.state.proposals("pending") + self.state.proposals("accepted"),
                        playtest=bool(getattr(adapter, "playtest", None)),
                        next_step="Fall back to $playtest if the adapter offers it; otherwise audit coverage and "
                                  "propose one missing probe.")
        suite, why = order[0]
        if self.state.claim("suite:" + suite.id, owner, "test", dict(revision=revision)):
            return dict(outcome="busy", suite=suite.id)
        if suite.desktop and self.state.claim("desktop", owner, "test", dict(suite=suite.id)):
            self.state.release(owner, "suite:" + suite.id)
            return dict(outcome="busy", suite=suite.id, reason="desktop in use")
        run_id = self.state.begin("test", suite.id, revision, source_ref, upstream, dict(
            suite=suite.record(), reason=why, harness=self.harness(), owner=owner, platform=platform.system(),
            hint=hint))
        artifacts = self.directory / "runs" / run_id
        artifacts.mkdir(parents=True, exist_ok=True)
        self.log(f"test {suite.id} ({why}) at {revision[:12]}; evidence: {artifacts}")
        state_name, detail = "blocked", None
        try:
            prepared = self.prepare(adapter, suite, checkout, revision, run_id, owner)
            if suite.framework == "hspec":
                paths = self._enumerate(suite, prepared, run_id, owner, artifacts)
                self.state.enumerated(suite.record(), suite.identity, revision, paths, upstream)
            else:
                self.state.enumerated(suite.record(), suite.identity, revision, list(suite.checks), upstream)
            selected = [r["id"] for r in self.state.tests(suite=suite.id) if r["status"] != "retired"]
            self.state.update_run(run_id, self.state.run(run_id)["document"] | dict(
                prepared=dict(argv=prepared.argv, cwd=prepared.cwd, provenance=prepared.provenance,
                              wrapper=prepared.wrapper), selected=len(selected)))
            state_name, detail = self._trials(suite, prepared, run_id, owner, artifacts, selected, 1, True)
            if upstream:
                self.state.tested(suite.id, suite.identity)
        except LabError as error:
            state_name, detail = "blocked", str(error)
        except KeyboardInterrupt:
            state_name, detail = "interrupted", "user interrupted; owned processes stopped"
        finally:
            for trial in self.state.trials(run_id):
                if trial["state"] == "running":
                    self.state.finish_trial(run_id, trial["number"], dict(outcome="interrupted"), {})
            outcomes = Counter(r["outcome"] for r in self.state.results(run_id))
            summary = dict(counts=dict(Counter(t["state"] for t in self.state.trials(run_id))),
                           tests=dict(outcomes), reason=detail)
            self.state.finish(run_id, state_name, summary)
            self.state.release(owner, "suite:" + suite.id)
            if suite.desktop:
                self.state.release(owner, "desktop")
            run = self.state.run(run_id)
            facts = "\n".join([
                f"- Probe: `{suite.id}` — {suite.description}",
                f"- Selected because: {why}; one execution; state {state_name}" + (f" ({detail})" if detail else ""),
                f"- Test outcomes: {dict(outcomes)}",
                f"- Log: `runs/{run_id}/trial-0001.log`; result: `runs/{run_id}/result.json`",
            ])
            report.write(artifacts / "report.md", report.skeleton(run, self.source(revision, source_ref), facts))
            atomic_json(artifacts / "result.json", run)
        return dict(outcome=state_name, run_id=run_id, suite=suite.id, revision=revision, reason=why,
                    candidate=not upstream, summary=summary, log=str(artifacts / "trial-0001.log"),
                    report=str(artifacts / "report.md"), skipped=skipped,
                    next_step=f"Interpret the log, complete {artifacts / 'report.md'}, then "
                              f"`quruntul report attach {run_id}`.")

    # -- reports --------------------------------------------------------------

    def attach(self, run_id: str) -> dict:
        run = self.state.run(run_id)
        path = self.directory / "runs" / run_id / "report.md"
        parsed = report.parse(path.read_text())
        if parsed["front"].get("run_id") != run_id:
            raise LabError("the report names another run")
        ids = self.state.add_observations(run_id, parsed["observations"])
        self.state.update_run(run_id, run["document"] | dict(report_sha256=parsed["sha256"]), run["state"])
        return dict(outcome="attached", run_id=run_id, interpretation=parsed["front"]["interpretation_status"],
                    observations=ids)

    # -- views ----------------------------------------------------------------

    def counts(self) -> dict:
        return dict(Counter(r["status"] for r in self.state.tests()))

    def render(self) -> Path:
        """ledger.md: a regenerated view of the ledger for humans. Never an input."""
        tests = self.state.tests()
        by_suite: dict[str, Counter] = {}
        for row in tests:
            by_suite.setdefault(row["suite"], Counter())[row["status"]] += 1
        lines = ["# Quruntul ledger", "", f"Generated {utc()} from ledger.sqlite3. Do not edit.", "",
                 f"Tests: {dict(Counter(r['status'] for r in tests))}", "", "## Suites", "",
                 "| Suite | new | stable | flaky | failing | fixing | pending | retired | enumerated | last probe run |",
                 "|---|---|---|---|---|---|---|---|---|---|"]
        for suite in self.state.suites():
            c = by_suite.get(suite["id"], Counter())
            lines.append(f"| {suite['id']} | {c['new']} | {c['stable']} | {c['flaky']} | {c['failing']} | {c['fixing']} | "
                         f"{c['pending']} | {c['retired']} | {suite['enumerated'] or '—'} | "
                         f"{suite['last_test_run'] or '—'} |")
        lines += ["", "## Flaky, failing and fixing", "", "| Test | Status | Failures / trials | Last failure | PR |",
                  "|---|---|---|---|---|"]
        for row in tests:
            if row["status"] in ("flaky", "failing", "fixing"):
                lines.append(f"| {row['id']} | {row['status']} | {row['failures']}/{row['trials']} | "
                             f"{row['last_failure'] or '—'} | {row['pr'] or '—'} |")
        lines += ["", "## Recent runs", "", "| Run | Lane | Suite | Started | State | Revision |",
                  "|---|---|---|---|---|---|"]
        for run in self.state.runs(limit=40):
            lines.append(f"| [{run['id'][:8]}](runs/{run['id']}/report.md) | {run['lane']} | {run['suite'] or '—'} | "
                         f"{run['started']} | {run['state']} | `{run['revision'][:12]}` |")
        live = self.state.claims()
        lines += ["", "## Active claims", ""] + ([f"- `{c['resource']}` — {c['owner']} ({c['lane']})" for c in live]
                                                  or ["None."])
        deferrals = self.state.deferred()
        lines += ["", "## Deferrals", ""] + ([f"- **{k}**: {d['reason']}; resume when {d['resume_when']}"
                                              for k, d in deferrals.items()] or ["None."])
        open_obs = self.state.observations("open")
        lines += ["", "## Open observations", ""] + ([f"- `{o['id']}` — {o['title']}" for o in open_obs] or ["None."])
        path = self.directory / "ledger.md"
        from quruntul.common import atomic_text
        atomic_text(path, "\n".join(lines) + "\n")
        return path

    def status(self) -> dict:
        self.recover()
        return dict(ledger=str(self.render()), directory=str(self.directory), tests=self.counts(),
                    suites=len(self.state.suites()), active_runs=[
                        dict(id=r["id"], lane=r["lane"], suite=r["suite"], started=r["started"])
                        for r in self.state.runs(active=True)],
                    claims=[dict(resource=c["resource"], owner=c["owner"], lane=c["lane"]) for c in self.state.claims()],
                    open_observations=len(self.state.observations("open")),
                    proposals=[dict(id=p["id"], target=p["target_id"], status=p["status"])
                               for p in self.state.proposals() if p["status"] in ("pending", "accepted")],
                    deferrals=self.state.deferred())

    def checkouts(self) -> list[dict]:
        base = self.common.parent.parent / f".{self.common.parent.name}-quruntul"
        active = {r["revision"] for r in self.state.runs(active=True, limit=1000)}
        found = []
        for path in sorted(base.glob("*")) if base.exists() else []:
            found.append(dict(path=str(path), revision=path.name, active=path.name in active))
        return found


SHAKEDOWN_PROBLEMS = ("build-failed", "enumeration-failed", "incomplete", "failed", "unreported")


def _classify(entry: dict, document: dict, number: int, tests: dict, outcomes: dict) -> None:
    """Every problem one shakedown trial shows. A trial completed when its process passed or failed."""
    outcome = document.get("outcome")
    completed = outcome in ("passed", "failed")
    log = document.get("log")
    entry["trial"] = dict(number=number, outcome=outcome, log=log, seed=document.get("seed"))
    problems = entry["problems"]
    if not completed:
        detail = document.get("error") or document.get("reason") or (
            f"exit status {document['returncode']}" if document.get("returncode") is not None else outcome)
        problems.append(dict(kind="incomplete", outcome=outcome, detail=detail, log=log,
                             tests=[t for t in tests if outcomes.get(t, "incomplete") == "incomplete"]))
    failed = [t for t in tests if outcomes.get(t) == "failed"]
    if failed:
        report_path = Path(entry["artifacts"]) / "trial-0001.failures"
        problems.append(dict(kind="failed", tests=failed, log=log,
                             failure_report=str(report_path) if report_path.exists() else None))
    if completed:
        unreported = [t for t in tests if outcomes.get(t, "missing") == "missing"]
        if unreported:
            problems.append(dict(kind="unreported", tests=unreported, log=log))
    not_passed = {kind: [t for t in tests if outcomes.get(t) == kind] for kind in ("pending", "unproven")}
    entry["not_passed"] = {kind: found for kind, found in not_passed.items() if found}


def _headline(entry: dict) -> dict:
    """A suite's result is its first problem in the fixed order, or clean."""
    entry["problems"].sort(key=lambda p: SHAKEDOWN_PROBLEMS.index(p["kind"]))
    entry["result"] = entry["problems"][0]["kind"] if entry["problems"] else "clean"
    return entry


def _problem_text(problem: dict) -> str:
    kind = problem["kind"]
    if kind in ("build-failed", "enumeration-failed"):
        return _line(problem["reason"])
    if kind == "incomplete":
        return (f"the trial ended {problem['outcome']} ({_line(problem['detail'])}); "
                f"{len(problem['tests'])} listed tests have no result")
    if kind == "failed":
        return f"{len(problem['tests'])} tests reported failure"
    return f"{len(problem['tests'])} listed tests reported no result in the completed trial"


def _line(text) -> str:
    return " ".join(str(text).split())


def _logs(directory: Path) -> list[str]:
    return [str(p) for p in sorted(directory.glob("*.log"))]


def _retained(prefix: Path) -> dict:
    """The guardian's own record of an interrupted trial, when it wrote one; the outcome is `interrupted`."""
    try:
        result = json.loads(Path(str(prefix) + ".result.json").read_text())
    except (OSError, ValueError):
        result = dict(log=str(prefix) + ".log")
    if result.get("outcome"):
        result["guardian_outcome"] = result["outcome"]
    result.update(outcome="interrupted", reason="the shakedown was interrupted; owned processes stopped")
    return result


def _launch(prepared, argv: list[str]) -> list[str]:
    """Drop the executable when the wrapper starts it itself."""
    return argv if prepared.launches_executable else argv[1:]


def _environment(extra: dict | None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED and not k.startswith("HSPEC_")}
    env.update(extra or {})
    return env


def _epoch(stamp: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(stamp).timestamp()


def _pr_merged(root: Path, pr: str) -> bool:
    if not shutil.which("gh"):
        return False
    try:
        state = subprocess.run(["gh", "pr", "view", pr, "--json", "state", "--jq", ".state"], cwd=root,
                               capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return state.returncode == 0 and state.stdout.strip() == "MERGED"


def new_owner(lane: str) -> str:
    return f"{lane}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def sha(path: Path) -> str:
    return file_hash(path)
