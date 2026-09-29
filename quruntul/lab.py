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

    def _runner(self, run_id: str, owner: str):
        artifacts = self.directory / "runs" / run_id

        def run(argv, name, timeout, cwd, environment):
            env = _environment(environment)
            return process.run(argv, cwd, env, artifacts / name, timeout,
                               lambda: self.state.run_heartbeat(run_id, owner))
        return run

    def prepare(self, adapter, suite, checkout, revision, run_id, owner) -> adapters.Prepared:
        self.adapter, self.adapter_ctx = adapter, adapters.Context(checkout, revision, self.directory / "runs" / run_id,
                                                                   self._runner(run_id, owner), self.log)
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
            ctx = adapters.Context(checkout, revision, self.directory / "runs" / run_id, self._runner(run_id, owner),
                                   self.log)
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
            measured = self._decide(suite, run_id, revision, selected, upstream, explicit)
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
            summary = dict(counts=counts, reason=detail, planned_trials=trials, selected=len(selected),
                           measured=measured, newly_flaky=newly_flaky,
                           interpretation=("blocked" if state_name == "blocked" else
                                           "observations" if newly_flaky or any(m["failures"] for m in measured.values()) else
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
            name = f"trial-{number:04}"
            seed = random.SystemRandom().randrange(1, 2 ** 31)
            env = dict(prepared.environment, QURUNTUL_TRIAL=str(number), QURUNTUL_SEED=str(seed),
                       QURUNTUL_PROBE_RESULT=str(artifacts / f"{name}.checks.json"),
                       QURUNTUL_TRIAL_PREFIX=str(artifacts / name))
            env.update(self._hook("trial_env", suite, dict(number=number, prefix=str(artifacts / name), seed=seed)) or {})
            if suite.framework == "hspec":
                failure_report = artifacts / f"{name}.failures"
                argv = prepared.wrapper + _launch(prepared, hspec.trial_argv(
                    prepared.argv[0], None if whole else paths, seed, str(failure_report), suite.rts,
                    prepared.argv[1:]))
            else:
                failure_report = None
                argv = prepared.wrapper + prepared.argv
            self.state.start_trial(run_id, number, dict(argv=argv, seed=seed, started=utc()))
            result = process.run(argv, Path(prepared.cwd), _environment(env), artifacts / name, suite.trial_seconds,
                                 lambda: self.state.run_heartbeat(run_id, owner))
            result["seed"] = seed
            outcomes = self._outcomes(suite, result, selected, failure_report, artifacts / f"{name}.checks.json",
                                      dict(number=number, prefix=str(artifacts / name), seed=seed))
            self.state.finish_trial(run_id, number, result, outcomes)
            failed = sum(1 for o in outcomes.values() if o == "failed")
            self.log(f"{suite.id} {number}/{trials}: {result['outcome']}, {failed} failed of {len(selected)} "
                     f"({result['duration_seconds']:.1f}s)")
            if result["outcome"] in ("interrupted", "harness-error", "setup-error"):
                return ("interrupted" if result["outcome"] == "interrupted" else "blocked"), result["outcome"]
        return state_name, detail

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
            reported = hspec.parse_checks(Path(result["log"]).read_text(errors="replace"))
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

    def _decide(self, suite, run_id, revision, selected, upstream, explicit) -> dict:
        """Counters for every selected test; status changes only for an upstream-head batch."""
        results = self.state.results(run_id)
        trials = {t["number"]: t for t in self.state.trials(run_id)}
        measured = {}
        for test_id in selected:
            mine = [r for r in results if r["test_id"] == test_id]
            passes = sum(1 for r in mine if r["outcome"] == "passed")
            failures = sum(1 for r in mine if r["outcome"] == "failed")
            complete = sum(1 for r in mine if r["outcome"] in ("passed", "failed", "pending"))
            outcome = "failed" if failures else ("passed" if complete and passes == complete else "incomplete")
            self.state.record_measurement(test_id, run_id, revision, complete, failures, outcome)
            row = self.state.test(test_id)
            became = None
            if upstream:
                planned = len(trials)
                if failures and row["status"] in ("new", "stable", "fixing"):
                    became = "flaky"
                elif (not failures and row["status"] in ("new", "fixing") and passes == planned
                      and all(t["state"] in ("passed", "failed") for t in trials.values())):
                    became = "stable"
                elif (not failures and not passes and row["status"] == "new" and complete == planned
                      and all(t["state"] in ("passed", "failed") for t in trials.values())):
                    # Pending in every trial: this environment never exercises it.
                    # It is not measured, so it is neither stable nor new work.
                    became = "pending"
                if became:
                    self.state.set_status(test_id, became, f"flake batch {run_id}: {failures} failed of {complete}",
                                          dict(run=run_id, revision=revision))
            measured[test_id] = dict(passes=passes, failures=failures, complete=complete, became=became,
                                     failing_trials=[r["number"] for r in mine if r["outcome"] == "failed"])
        return measured

    def _flake_report(self, run_id, suite, summary, detail):
        run = self.state.run(run_id)
        artifacts = self.directory / "runs" / run_id
        observations = []
        for test_id, m in sorted(summary["measured"].items()):
            if not m["failures"]:
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
        if summary["interpretation"] == "blocked":
            observations.append(dict(
                title=f"flake batch of {suite.id} blocked", area=suite.area or suite.id, kind="harness",
                tests="none", evidence=f"`runs/{run_id}/` logs; {detail}", expected="the suite builds and runs",
                observed=str(detail), confidence="medium", follow_up="inspect the build/enumeration log"))
        facts = "\n".join([
            f"- Suite: `{suite.id}` ({suite.kind}, {suite.framework}); selected because: {run['document']['reason']}",
            f"- Trials: {summary['counts']} of {summary['planned_trials']} planned; selected tests: {summary['selected']}",
            f"- Became stable: {sum(1 for m in summary['measured'].values() if m['became'] == 'stable')}; "
            f"became flaky: {len(summary['newly_flaky'])}",
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
                 "| Suite | new | stable | flaky | fixing | pending | retired | enumerated | last probe run |",
                 "|---|---|---|---|---|---|---|---|---|"]
        for suite in self.state.suites():
            c = by_suite.get(suite["id"], Counter())
            lines.append(f"| {suite['id']} | {c['new']} | {c['stable']} | {c['flaky']} | {c['fixing']} | "
                         f"{c['pending']} | {c['retired']} | {suite['enumerated'] or '—'} | "
                         f"{suite['last_test_run'] or '—'} |")
        lines += ["", "## Flaky and fixing", "", "| Test | Status | Failures / trials | Last failure | PR |",
                  "|---|---|---|---|---|"]
        for row in tests:
            if row["status"] in ("flaky", "fixing"):
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
