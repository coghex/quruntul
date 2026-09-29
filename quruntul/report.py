"""The quruntul-result/v1 report: one Markdown file per run, validated on attach.

The engine writes the factual sections; the agent writes interpretation and
observations. Attaching validates the whole file and ingests each OBS-nnn
section into the ledger, where $assess-tests picks it up.
"""
from __future__ import annotations

import re
from pathlib import Path

from quruntul.common import LabError, atomic_text, text_hash

SCHEMA = "quruntul-result/v1"
STATUSES = ("clean", "observations", "inconclusive", "blocked")
KINDS = ("product", "harness", "infrastructure", "environment", "flaky-test", "expected", "uncertain")
FIELDS = ("Area", "Kind", "Tests", "Evidence", "Expected", "Observed", "Confidence", "Suggested follow-up")
PLACEHOLDER = "<REPLACE:"

OBSERVATION_TEMPLATE = """### OBS-{number:03} — {title}

- Area: {area}
- Kind: {kind}
- Tests: {tests}
- Evidence: {evidence}
- Expected: {expected}
- Observed: {observed}
- Confidence: {confidence}
- Suggested follow-up: {follow_up}
"""


def skeleton(run: dict, source: dict, facts: str, status: str | None = None,
             interpretation: str | None = None, limitations: str | None = None,
             observations: list[dict] | None = None) -> str:
    """A report with the engine's facts filled in and the agent's parts marked."""
    lines = [
        "---",
        f"schema: {SCHEMA}",
        f"run_id: {run['id']}",
        f"lane: {run['lane']}",
        f"suite: {run.get('suite') or ''}",
        f"revision: {run['revision']}",
        f"interpretation_status: {status or '<REPLACE: clean | observations | inconclusive | blocked>'}",
        "---",
        "",
        f"# {run['lane']} run: {run.get('suite') or 'no suite'} at {run['revision'][:12]}",
        "",
        "## Source version",
        "",
        f"- Ref: `{source.get('ref', '')}`",
        f"- Commit: `{run['revision']}`",
        f"- Subject: {source.get('subject', '')}",
        f"- Committed: {source.get('committed', '')}",
        f"- Upstream head when run: {'yes' if run.get('upstream') else 'no (candidate evidence)'}",
        "",
        "## Execution",
        "",
        facts.rstrip(),
        "",
        "## Interpretation",
        "",
        interpretation or "<REPLACE: what the evidence shows, separating command success, assertion outcomes, observed behaviour and your judgement>",
        "",
        "## Limitations",
        "",
        limitations or "<REPLACE: what this run could not establish, even when clean>",
        "",
        "## Observations",
        "",
    ]
    if observations is None:
        lines.append("<REPLACE: one OBS-nnn section per independent observation, using the fields below; "
                     "write 'None.' for a clean run>")
        lines.append("")
        lines.append(OBSERVATION_TEMPLATE.format(
            number=1, title="<REPLACE: short title>", area="<REPLACE>", kind="<REPLACE: " + " | ".join(KINDS) + ">",
            tests="<REPLACE: test ids, or none>", evidence="<REPLACE: artifact paths, counts, log lines>",
            expected="<REPLACE>", observed="<REPLACE>", confidence="<REPLACE: high | medium | low>",
            follow_up="<REPLACE>"))
    elif not observations:
        lines.append("None.")
    else:
        for number, observation in enumerate(observations, 1):
            lines.append(OBSERVATION_TEMPLATE.format(number=number, **observation))
    return "\n".join(lines).rstrip() + "\n"


def parse(text: str) -> dict:
    """Validate a report and return its frontmatter and observations."""
    if PLACEHOLDER in text:
        raise LabError("the report still contains <REPLACE: ...> markers")
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not match:
        raise LabError("the report needs its YAML frontmatter")
    front = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        front[key.strip()] = value.strip()
    if front.get("schema") != SCHEMA:
        raise LabError(f"schema must be {SCHEMA}")
    status = front.get("interpretation_status")
    if status not in STATUSES:
        raise LabError("interpretation_status must be one of " + ", ".join(STATUSES))
    body = text[match.end():]
    for heading in ("## Source version", "## Execution", "## Interpretation", "## Limitations", "## Observations"):
        if heading not in body:
            raise LabError(f"missing section {heading}")
    section = body.split("## Observations", 1)[1]
    blocks = re.split(r"^### ", section, flags=re.M)[1:]
    observations = []
    for expected_number, block in enumerate(blocks, 1):
        head, _, rest = block.partition("\n")
        found = re.match(r"OBS-(\d{3}) — (.+)", head.strip())
        if not found or int(found.group(1)) != expected_number:
            raise LabError(f"observation {expected_number} must be headed '### OBS-{expected_number:03} — title'")
        fields = {}
        for line in rest.splitlines():
            item = re.match(r"- ([A-Za-z -]+): (.*)", line.strip())
            if item and item.group(1) in FIELDS:
                fields[item.group(1)] = item.group(2).strip()
        missing = [f for f in FIELDS if not fields.get(f)]
        if missing:
            raise LabError(f"OBS-{expected_number:03} lacks: " + ", ".join(missing))
        if fields["Kind"] not in KINDS:
            raise LabError(f"OBS-{expected_number:03} kind must be one of " + ", ".join(KINDS))
        if fields["Confidence"] not in ("high", "medium", "low"):
            raise LabError(f"OBS-{expected_number:03} confidence must be high, medium or low")
        observations.append(dict(title=found.group(2).strip(), area=fields["Area"],
                                 **{k.lower().replace(" ", "_").replace("-", "_"): v
                                    for k, v in fields.items() if k != "Area"}))
    if status == "clean" and observations:
        raise LabError("a clean report has no observations")
    if status in ("observations", "blocked") and not observations:
        raise LabError(f"a {status} report needs at least one observation")
    return dict(front=front, observations=observations, sha256=text_hash(text))


def write(path: Path, text: str) -> None:
    atomic_text(Path(path), text)
