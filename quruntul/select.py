"""Pure selection. No I/O: callers pass the ledger's view and get an order back."""
from __future__ import annotations

from datetime import datetime, timezone

FLAKE_REASONS = ("verify-fix", "new-tests", "never-enumerated", "changed")
TEST_REASONS = ("never-tested", "changed", "stale")


def _blocked(suite, deferred, claims, platform, owner, allow_desktop=True):
    if suite.id in deferred:
        return "deferred: " + deferred[suite.id]["reason"]
    if platform not in suite.platforms:
        return "platform-inapplicable"
    holder = claims.get("suite:" + suite.id)
    if holder and holder["owner"] != owner:
        return f"claimed by {holder['owner']} ({holder['lane']})"
    if suite.desktop:
        if not allow_desktop:
            return "desktop-requires-explicit-request"
        holder = claims.get("desktop")
        if holder and holder["owner"] != owner:
            return f"desktop in use by {holder['owner']}"
    return None


def flake_order(suites, ledger_suites, tests, deferred, claims, platform, owner, merged_fixing=()):
    """Suites worth a flake batch, best first, and why every other suite was skipped.

    `ledger_suites` maps suite id -> ledger row; `tests` maps suite id -> test rows.
    `merged_fixing` holds test ids whose deflake PR has merged.
    """
    order, skipped = [], {}
    for suite in suites:
        reason = _blocked(suite, deferred, claims, platform, owner)
        if reason:
            skipped[suite.id] = reason
            continue
        rows = tests.get(suite.id, [])
        known = ledger_suites.get(suite.id)
        if any(r["status"] == "fixing" and r["id"] in merged_fixing for r in rows):
            why = "verify-fix"
        elif any(r["status"] == "new" for r in rows):
            why = "new-tests"
        elif not known or not known.get("enumerated"):
            why = "never-enumerated"
        elif known.get("identity") != suite.identity:
            why = "changed"
        else:
            skipped[suite.id] = "no new tests since its last enumeration"
            continue
        order.append((FLAKE_REASONS.index(why), -suite.priority, suite.id, suite, why))
    order.sort(key=lambda item: item[:3])
    return [(suite, why) for *_, suite, why in order], skipped


def test_order(suites, ledger_suites, deferred, claims, platform, owner, refresh_days, now=None, hint=None):
    """Probes worth observing, best first. CI suites are never candidates."""
    now = now or datetime.now(timezone.utc)
    order, skipped = [], {}
    needle = (hint or "").lower().strip()
    for suite in suites:
        if suite.kind != "probe":
            continue
        if needle and needle not in " ".join((suite.id, suite.area, suite.description)).lower():
            skipped[suite.id] = "outside the requested area"
            continue
        reason = _blocked(suite, deferred, claims, platform, owner, allow_desktop=False)
        if reason:
            skipped[suite.id] = reason
            continue
        known = ledger_suites.get(suite.id) or {}
        last = known.get("last_test_run")
        if not last:
            why = "never-tested"
        elif known.get("last_test_identity") != suite.identity:
            why = "changed"
        elif (now - datetime.fromisoformat(last)).total_seconds() > refresh_days * 86400:
            why = "stale"
        else:
            skipped[suite.id] = f"observed {last}; unchanged and within {refresh_days} days"
            continue
        order.append((TEST_REASONS.index(why), -suite.priority, last or "", suite.id, suite, why))
    order.sort(key=lambda item: item[:4])
    return [(suite, why) for *_, suite, why in order], skipped


def deflake_order(tests, claims, deferred, owner):
    """Flaky tests nobody is working on: highest observed failure rate, then most recent failure."""
    ranked = []
    for row in tests:
        if row["status"] != "flaky" or row["id"] in deferred or row["suite"] in deferred:
            continue
        holder = claims.get("test:" + row["id"])
        if holder and holder["owner"] != owner:
            continue
        rate = row["failures"] / row["trials"] if row["trials"] else 1.0
        ranked.append((-rate, _negated(row["last_failure"] or ""), row["id"], row))
    ranked.sort(key=lambda item: item[:3])
    return [row for *_, row in ranked]


def _negated(text: str) -> str:
    # Sort newer ISO timestamps first without parsing them.
    return "".join(chr(0x10FFFF - ord(c)) for c in text)
