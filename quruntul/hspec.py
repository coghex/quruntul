"""Hspec enumeration, exact selection and per-example results.

Hspec keeps the assertions; this module only reads what Hspec reports. The
`checks` formatter prints every spec item with a trailing mark and indents each
nesting level by two spaces, so a run's output names every example it ran and
how it ended. `--failure-report` additionally names each failed example by its
exact path, which is what decides a failure when the two could disagree.
"""
from __future__ import annotations

import re

PASSED, FAILED, PENDING = "passed", "failed", "pending"

# Unicode marks and their --no-unicode ASCII equivalents.
_MARKS = {"✔": PASSED, "✘": FAILED, "‐": PENDING, "v": PASSED, "x": FAILED, "-": PENDING}
_ITEM = re.compile(r"^(?P<indent> *)(?P<text>.*?) \[(?P<mark>✔|✘|‐|v|x|-)\]\s*$")
_ENDS = ("Failures:", "Finished in ", "Randomized with seed")


def base_argv(executable: str) -> list[str]:
    """Options every invocation carries: no ambient .hspec, no colour, one formatter."""
    return [executable, "--ignore-dot-hspec", "--no-color", "--unicode", "--format=checks"]


def select_argv(paths: list[str]) -> list[str]:
    """Exact selection. Hspec matches a pattern against '/'-joined paths wrapped
    in slashes, so '/A/B/example/' selects that example and nothing whose path
    merely contains it. An example text that itself contains '/' can still
    over-select a sibling; results are read only for the paths asked for."""
    argv: list[str] = []
    for path in paths:
        argv += ["--match", "/" + path + "/"]
    return argv


def trial_argv(executable: str, paths: list[str] | None, seed: int, failure_report: str,
               rts: list[str] | None = None, extra: list[str] | None = None) -> list[str]:
    argv = base_argv(executable) + ["--fail-on=empty", f"--seed={seed}", f"--failure-report={failure_report}"]
    if paths:
        argv += select_argv(paths)
    argv += list(extra or [])
    if rts:
        argv += ["+RTS", *rts, "-RTS"]
    return argv


def enumerate_argv(executable: str, extra: list[str] | None = None) -> list[str]:
    return base_argv(executable) + ["--dry-run", *(extra or [])]


def parse_checks(text: str) -> dict[str, str]:
    """Map each reported example path ('A/B/example') to passed/failed/pending.

    Group lines carry no mark and set the nesting stack. Lines after the
    formatter's summary (failure details, timing) are ignored. A carriage
    return means the formatter redrew the line; only its final state counts.
    """
    results: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for raw in text.splitlines():
        line = raw.rsplit("\r", 1)[-1].rstrip("\n")
        if not line.strip():
            continue
        if line.startswith(_ENDS):
            break
        indent = len(line) - len(line.lstrip(" "))
        match = _ITEM.match(line)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if match:
            path = "/".join([name for _, name in stack] + [match.group("text").strip()])
            results[path] = _MARKS[match.group("mark")]
        else:
            stack.append((indent, line.strip()))
    return results


def enumerate_examples(text: str) -> list[str]:
    """Every example a --dry-run printed, in suite order."""
    return list(parse_checks(text))


def _haskell_string(source: str, index: int) -> tuple[str, int]:
    """Read one Haskell string literal starting at the opening quote."""
    assert source[index] == '"'
    index += 1
    out: list[str] = []
    simple = {"n": "\n", "t": "\t", "\\": "\\", '"': '"', "'": "'", "r": "\r", "a": "\a",
              "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
    names = {"NUL": 0, "SOH": 1, "STX": 2, "ETX": 3, "EOT": 4, "ENQ": 5, "ACK": 6, "BEL": 7, "BS": 8,
             "HT": 9, "LF": 10, "VT": 11, "FF": 12, "CR": 13, "SO": 14, "SI": 15, "DLE": 16, "DC1": 17,
             "DC2": 18, "DC3": 19, "DC4": 20, "NAK": 21, "SYN": 22, "ETB": 23, "CAN": 24, "EM": 25,
             "SUB": 26, "ESC": 27, "FS": 28, "GS": 29, "RS": 30, "US": 31, "SP": 32, "DEL": 127}
    while True:
        char = source[index]
        if char == '"':
            return "".join(out), index + 1
        if char != "\\":
            out.append(char)
            index += 1
            continue
        index += 1
        char = source[index]
        if char == "&":
            index += 1
        elif char.isdigit():
            end = index
            while end < len(source) and source[end].isdigit():
                end += 1
            out.append(chr(int(source[index:end])))
            index = end
        elif char == "x":
            end = index + 1
            while end < len(source) and source[end] in "0123456789abcdefABCDEF":
                end += 1
            out.append(chr(int(source[index + 1:end], 16)))
            index = end
        elif char == "^":
            out.append(chr(ord(source[index + 1]) - 64))
            index += 2
        elif char.isspace():
            # A string gap: backslash, whitespace, backslash.
            index = source.index("\\", index) + 1
        else:
            name = next((n for n in sorted(names, key=len, reverse=True) if source.startswith(n, index)), None)
            if name:
                out.append(chr(names[name]))
                index += len(name)
            else:
                out.append(simple.get(char, char))
                index += 1


def parse_failure_report(text: str) -> list[str]:
    """Failed example paths from Hspec's --failure-report file (a Haskell `show`)."""
    marker = text.find("failureReportPaths")
    if marker < 0:
        raise ValueError("not an Hspec failure report")
    index = text.index("[", marker) + 1
    paths: list[str] = []
    while True:
        while text[index] in " ,\n":
            index += 1
        if text[index] == "]":
            return paths
        if text[index] != "(":
            raise ValueError("unexpected failure report shape")
        index = text.index("[", index) + 1
        groups: list[str] = []
        while True:
            while text[index] in " ,\n":
                index += 1
            if text[index] == "]":
                index += 1
                break
            value, index = _haskell_string(text, index)
            groups.append(value)
        while text[index] in " ,\n":
            index += 1
        leaf, index = _haskell_string(text, index)
        index = text.index(")", index) + 1
        paths.append("/".join(groups + [leaf]))
