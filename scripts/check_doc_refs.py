"""Check (and optionally re-anchor) the ``file:line`` references in the docs.

Run::

    uv run python scripts/check_doc_refs.py            # report drift, exit 1 if any
    uv run python scripts/check_doc_refs.py --apply     # rewrite them in place

Why this exists: every claim in ``docs/architecture/*.md`` carries a
``文件:行号`` reference (AGENTS.md section 6), and inserting or deleting lines in
``app/`` silently invalidates them. The drift is found the way AGENTS.md
describes -- build an old->new line map from ``difflib.SequenceMatcher`` opcodes
between ``HEAD`` and the working tree, then translate the referenced line.

**Run it once per uncommitted batch of code changes.** The map is
``HEAD -> working tree``, so it assumes the document still holds *committed*
(HEAD) coordinates. Running ``--apply`` twice before committing shifts every
reference a second time and corrupts them -- when that happens, restore the docs
(``git checkout -- docs/``) and run it once. Refs you hand-wrote while editing
(already in working-tree coordinates) get shifted too, so after an ``--apply``
re-check the refs into the files you touched, by symbol, not by number.

Two details that make the difference between "useful" and "wrong":

* continuation refs are bare (``:236`` after a full ``app/parsing/markdown.py:161``
  in the same line/row) and inherit the file from their context -- a checker that
  only understands full refs misses most of them;
* a referenced line can be *rewritten* rather than moved (opcode ``replace``), and
  then there is no new line to point at: those are reported for a human to
  re-anchor, never guessed.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Documents whose ``file:line`` references are checked.
DOC_GLOBS = ("README.md", "AGENTS.md", "docs/**/*.md")

#: ``app/x.py:12`` or ``app/x.py:12-20`` (optionally in backticks).
FULL_REF = re.compile(
    r"`?(?P<path>(?:[\w.-]+/)*[\w.-]+\.(?:py|md|toml|yaml|yml|cfg|example)):"
    r"(?P<start>\d+)(?:-(?P<end>\d+))?`?"
)
#: ``:12`` / ``:12-20`` -- the file comes from the surrounding context.
BARE_REF = re.compile(r"(?<![\w:])`?:(?P<start>\d+)(?:-(?P<end>\d+))?`?")
#: A fenced code block: hints are inside them too, so they are scanned as well.
FENCE = re.compile(r"^\s*```")


@dataclass
class Reference:
    doc: str
    line_no: int
    path: str
    start: int
    end: int | None
    text: str
    bare: bool
    new_start: int | None = None
    new_end: int | None = None

    @property
    def drifted(self) -> bool:
        return self.new_start is not None and (
            self.new_start != self.start
            or (self.end is not None and self.new_end != self.end)
        )

    @property
    def rewritten(self) -> bool:
        """The referenced line was replaced, so there is no line to point at."""
        return self.new_start is None


@dataclass
class DocReport:
    path: str
    references: list[Reference] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# line maps: HEAD -> working tree
# --------------------------------------------------------------------------- #


def head_lines(rel: str) -> list[str] | None:
    result = subprocess.run(
        ["git", "show", f"HEAD:{rel}"], cwd=ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        return None
    return result.stdout.split("\n")


def work_lines(rel: str) -> list[str] | None:
    path = ROOT / rel
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").split("\n")


def build_line_map(rel: str) -> dict[int, int | None] | None:
    """``{old line: new line}`` for one file; ``None`` where the line was rewritten."""
    old, new = head_lines(rel), work_lines(rel)
    if old is None or new is None:
        return None
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    mapping: dict[int, int | None] = {}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                mapping[i1 + offset + 1] = j1 + offset + 1
        elif tag in ("replace", "delete"):
            for index in range(i1, i2):
                mapping[index + 1] = None
    return mapping


# --------------------------------------------------------------------------- #
# reference extraction
# --------------------------------------------------------------------------- #


def source_contexts(doc_text: str) -> dict[int, str]:
    """The file a bare ``:NNN`` belongs to, per line.

    A full reference sets the context for the rest of its line. Inside a table or
    a code block the context carries on to the following lines, which is exactly
    how these documents are written (``| `pdf.py` | … | `:93-129` | …``).
    """
    contexts: dict[int, str] = {}
    current: str | None = None
    fenced = False
    for number, line in enumerate(doc_text.split("\n"), 1):
        if FENCE.match(line):
            fenced = not fenced
            contexts[number] = current or ""
            continue
        full = FULL_REF.search(line)
        if full:
            current = full.group("path")
        elif line.strip().startswith("|") and line.strip().endswith("|"):
            # A table row without its own path keeps the table's context, unless
            # it is a new table (a row naming a file name in backticks).
            name = re.search(r"^\|\s*`?([\w./-]+\.(?:py|md))`?\s*\|", line.strip())
            if name:
                current = name.group(1)
        elif not line.strip() and not fenced:
            current = None
        elif line.startswith("#") and "`" not in line:
            current = None
        contexts[number] = current or ""
    return contexts


def collect(doc: Path) -> DocReport:
    text = doc.read_text(encoding="utf-8")
    contexts = source_contexts(text)
    report = DocReport(path=str(doc.relative_to(ROOT)))
    for number, line in enumerate(text.split("\n"), 1):
        taken: list[tuple[int, int]] = []
        for match in FULL_REF.finditer(line):
            taken.append((match.start(), match.end()))
            report.references.append(
                Reference(
                    doc=report.path,
                    line_no=number,
                    path=match.group("path"),
                    start=int(match.group("start")),
                    end=int(match.group("end")) if match.group("end") else None,
                    text=match.group(0),
                    bare=False,
                )
            )
        for match in BARE_REF.finditer(line):
            if any(start <= match.start() < end for start, end in taken):
                continue
            context = contexts.get(number, "")
            if not context:
                continue
            report.references.append(
                Reference(
                    doc=report.path,
                    line_no=number,
                    path=context,
                    start=int(match.group("start")),
                    end=int(match.group("end")) if match.group("end") else None,
                    text=match.group(0),
                    bare=True,
                )
            )
    return report


# --------------------------------------------------------------------------- #
# checking
# --------------------------------------------------------------------------- #


#: Where a bare filename is looked for first (in this order).
SEARCH_ROOTS = ("app", "tests", "scripts", "infra", "migrations")


def resolve_path(path: str) -> Path | None:
    """Find a referenced file, tolerating the short forms the docs use.

    The docs write both ``app/parsing/markdown.py`` and plain ``markdown.py``;
    the short form is resolved against the usual roots first and then by name
    anywhere in the tree (skipping the virtualenv and caches).
    """
    candidate = ROOT / path
    if candidate.is_file():
        return candidate
    for folder in SEARCH_ROOTS:
        candidate = ROOT / folder / path
        if candidate.is_file():
            return candidate
    matches = _by_name().get(Path(path).name, [])
    if not matches:
        return None
    return matches[0]


#: Names never walked: the virtualenv alone is tens of thousands of files.
SKIP_DIRS = {".venv", "__pycache__", ".git", "node_modules", "logs", ".pytest_cache"}
_BY_NAME: dict[str, list[Path]] = {}


def _by_name() -> dict[str, list[Path]]:
    """``basename -> [paths]`` for the repository, built once, shortest first."""
    if _BY_NAME:
        return _BY_NAME
    for folder, subfolders, files in os.walk(ROOT):
        subfolders[:] = [name for name in subfolders if name not in SKIP_DIRS]
        for name in files:
            _BY_NAME.setdefault(name, []).append(Path(folder) / name)
    for paths in _BY_NAME.values():
        paths.sort(key=lambda found: (len(found.parts), str(found)))
    return _BY_NAME


def check(reference: Reference, maps: dict[str, dict[int, int | None] | None]) -> list[str]:
    problems: list[str] = []
    target = resolve_path(reference.path)
    if target is None:
        return [f"{reference.doc}:{reference.line_no} unknown file {reference.path!r}"]
    lines = target.read_text(encoding="utf-8").split("\n")
    for value in (reference.start, reference.end):
        if value is None:
            continue
        if value < 1 or value > len(lines):
            problems.append(
                f"{reference.doc}:{reference.line_no} {reference.path}:{value} is out of "
                f"range (file has {len(lines)} lines)"
            )
    rel = str(target.relative_to(ROOT)).replace("\\", "/")
    mapping = maps.get(rel)
    if mapping is None:
        return problems
    new_start = mapping.get(reference.start)
    new_end = mapping.get(reference.end) if reference.end is not None else None
    if new_start is None and reference.start not in mapping:
        return problems  # line did not exist at HEAD (a reference to new code)
    reference.new_start = new_start
    reference.new_end = new_end
    if reference.rewritten:
        problems.append(
            f"{reference.doc}:{reference.line_no} {reference.path}:{reference.start} was "
            f"rewritten -- re-anchor it by hand"
        )
    elif reference.drifted:
        problems.append(
            f"{reference.doc}:{reference.line_no} {reference.path}:{reference.start}"
            f"{'-' + str(reference.end) if reference.end else ''} -> "
            f"{reference.new_start}{'-' + str(reference.new_end) if reference.new_end else ''}"
        )
    return problems


def apply_fixes(report: DocReport) -> int:
    """Rewrite the drifted references of one document, longest first."""
    doc = ROOT / report.path
    text = doc.read_text(encoding="utf-8")
    changed = 0
    for reference in sorted(report.references, key=lambda r: -r.line_no):
        if not reference.drifted or reference.rewritten:
            continue
        replacement = f":{reference.new_start}"
        if reference.end is not None and reference.new_end is not None:
            replacement += f"-{reference.new_end}"
        if reference.bare:
            replacement = reference.text.replace(
                f":{reference.start}"
                + (f"-{reference.end}" if reference.end is not None else ""),
                replacement,
            )
        else:
            replacement = reference.text.replace(
                f":{reference.start}"
                + (f"-{reference.end}" if reference.end is not None else ""),
                replacement,
            )
        lines = text.split("\n")
        index = reference.line_no - 1
        line = lines[index]
        # Replace only this occurrence in this line: references are unique per
        # line except for repeated identical ones, which move the same way.
        lines[index] = line.replace(reference.text, replacement, 1)
        if lines[index] == line:
            continue
        text = "\n".join(lines)
        changed += 1
    doc.write_text(text, encoding="utf-8")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="rewrite drifted references")
    parser.add_argument("--json", action="store_true", help="print the collected references")
    args = parser.parse_args()

    docs: list[Path] = []
    for pattern in DOC_GLOBS:
        docs.extend(sorted(ROOT.glob(pattern)))
    docs = [doc for doc in docs if doc.is_file()]

    reports = [collect(doc) for doc in docs]
    maps: dict[str, dict[int, int | None] | None] = {}
    for report in reports:
        for reference in report.references:
            target = resolve_path(reference.path)
            if target is None:
                continue
            rel = str(target.relative_to(ROOT)).replace("\\", "/")
            if rel not in maps:
                maps[rel] = build_line_map(rel)

    problems: list[str] = []
    for report in reports:
        for reference in report.references:
            problems.extend(check(reference, maps))

    references = sum(len(report.references) for report in reports)
    drift = [problem for problem in problems if "->" in problem]
    broken = [problem for problem in problems if "->" not in problem]
    print(f"checked {references} reference(s) in {len(reports)} document(s)")
    print(f"  drifted : {len(drift)}")
    print(f"  broken  : {len(broken)}")
    for problem in drift:
        print(f"    {problem}")
    for problem in broken:
        print(f"    {problem}")

    if args.apply and drift:
        changed = sum(apply_fixes(report) for report in reports)
        print(f"rewrote {changed} reference(s); re-run without --apply to confirm")

    if args.json:
        payload = [
            {
                "doc": reference.doc,
                "line": reference.line_no,
                "ref": f"{reference.path}:{reference.start}",
                "new": reference.new_start,
                "bare": reference.bare,
            }
            for report in reports
            for reference in report.references
        ]
        print(json.dumps(payload, ensure_ascii=False))

    return 1 if (drift or broken) and not args.apply else 0


if __name__ == "__main__":
    raise SystemExit(main())
