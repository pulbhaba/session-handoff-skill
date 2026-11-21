#!/usr/bin/env python3
"""Read and write the session handoff file safely when several sessions or agents share it.

Every write takes an exclusive lock, reads the file, changes one entry and swaps the
new file in atomically, so concurrent writers wait in turn and never overwrite each
other. Readers need no lock: the file is always whole. `replace` and `remove` also
need the entry's `rev` from your last read, so an edit based on a stale read fails
instead of losing someone else's update.

Usage (run `handoff.py -h` for details):
  handoff.py list
  handoff.py get    <entry>
  handoff.py add    --topic "<topic>" [--date YYYY-MM-DD]   < body.md
  handoff.py update <entry> [--date YYYY-MM-DD]             < text.md
  handoff.py status <entry> "<new status>"
  handoff.py replace <entry> --rev <rev>                    < body.md
  handoff.py remove  <entry> --rev <rev>

<entry> is any text that appears in exactly one entry heading (e.g. a topic word).
File: .claude/session-handoff.md under the current working directory; override with HANDOFF_FILE.
Standard library only, Python 3.9+, macOS and Linux (fcntl).
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import os
import re
import signal
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Run commands from the workspace root. HANDOFF_FILE can point elsewhere.
DEFAULT_FILE = Path.cwd() / ".claude" / "session-handoff.md"
LOCK_TIMEOUT_SECONDS = int(os.environ.get("HANDOFF_LOCK_TIMEOUT", "30"))
SEPARATOR = "\n\n---\n\n"
STATUS_LINE = re.compile(r"^\*\*Status:\*\*[ \t]*(.*)$", re.MULTILINE)


class HandoffError(Exception):
    pass


@dataclass(frozen=True)
class Document:
    preamble: str
    entries: tuple[str, ...]


def handoff_path() -> Path:
    return Path(os.environ.get("HANDOFF_FILE", DEFAULT_FILE))


def heading(entry: str) -> str:
    return entry.split("\n", 1)[0]


def rev(entry: str) -> str:
    return hashlib.sha256(entry.encode()).hexdigest()[:10]


def status(entry: str) -> str:
    found = STATUS_LINE.search(entry)
    return found.group(1).strip() if found else "(no status)"


# --- parse and render: pure, round-trips the file byte for byte -----------------

def parse(text: str) -> Document:
    """Split at top-level `## ` headings (not inside code fences)."""
    lines = text.split("\n")
    starts, in_fence = [], False
    for index, line in enumerate(lines):
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence and line.startswith("## "):
            starts.append(index)
    if not starts:
        return Document(strip_separator(text), ())
    bounds = starts + [len(lines)]
    preamble = strip_separator("\n".join(lines[: starts[0]]))
    entries = tuple(
        strip_separator("\n".join(lines[a:b])) for a, b in zip(bounds, bounds[1:])
    )
    return Document(preamble, entries)


def strip_separator(chunk: str) -> str:
    chunk = chunk.rstrip()
    if chunk.endswith("\n---") or chunk == "---":
        chunk = chunk[: -3].rstrip()
    return chunk


def render(doc: Document) -> str:
    return SEPARATOR.join((doc.preamble, *doc.entries)) + "\n\n---\n"


def find(doc: Document, query: str) -> int:
    matches = [i for i, e in enumerate(doc.entries) if query.lower() in heading(e).lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise HandoffError(f"no entry heading contains {query!r}; run `list`")
    listing = "\n".join(f"  {heading(doc.entries[i])}" for i in matches)
    raise HandoffError(f"{query!r} matches {len(matches)} entries, be more specific:\n{listing}")


def with_entry(doc: Document, index: int, entry: str | None) -> Document:
    head, tail = doc.entries[:index], doc.entries[index + 1 :]
    middle = () if entry is None else (entry.strip(),)
    return Document(doc.preamble, head + middle + tail)


def check_rev(entry: str, given: str) -> None:
    if rev(entry) != given:
        raise HandoffError(
            f"entry changed since you read it (rev {given}, now {rev(entry)}); "
            "run `get` again, merge your change into the new text, and retry"
        )


# --- operations: Document -> Document ------------------------------------------

def op_add(doc: Document, topic: str, date: str, body: str) -> Document:
    if not STATUS_LINE.search(body):
        raise HandoffError("the body must contain a **Status:** line (see SKILL.md)")
    entry = f"## {date}: {topic.strip()}\n\n{body.strip()}"
    return Document(doc.preamble, (entry, *doc.entries))


def op_update(doc: Document, query: str, date: str, text: str) -> Document:
    index = find(doc, query)
    first, _, rest = doc.entries[index].partition("\n")
    entry = f"{first}\n\n**Update {date}:** {text.strip()}\n\n{rest.lstrip()}"
    return with_entry(doc, index, entry)


def op_status(doc: Document, query: str, new_status: str) -> Document:
    index = find(doc, query)
    entry = doc.entries[index]
    if not STATUS_LINE.search(entry):
        raise HandoffError("entry has no **Status:** line; use `replace`")
    line = "**Status:** " + " ".join(new_status.split())
    return with_entry(doc, index, STATUS_LINE.sub(lambda _: line, entry, count=1))


def op_replace(doc: Document, query: str, given_rev: str, body: str) -> Document:
    index = find(doc, query)
    old = doc.entries[index]
    check_rev(old, given_rev)
    body = body.strip()
    entry = body if body.startswith("## ") else f"{heading(old)}\n\n{body}"
    return with_entry(doc, index, entry)


def op_remove(doc: Document, query: str, given_rev: str) -> Document:
    index = find(doc, query)
    check_rev(doc.entries[index], given_rev)
    return with_entry(doc, index, None)


# --- I/O: lock, read, write atomically ------------------------------------------

def read_doc(path: Path) -> Document:
    return parse(path.read_text()) if path.exists() else Document("# Session handoff", ())


class Locked:
    """Exclusive lock on a sidecar file; writers queue on it in the kernel."""

    def __init__(self, path: Path):
        self.lock_path = path.with_name(f".{path.name}.lock")

    def __enter__(self):
        self.handle = open(self.lock_path, "a")
        signal.signal(signal.SIGALRM, self._timeout)
        signal.alarm(LOCK_TIMEOUT_SECONDS)
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX)
        finally:
            signal.alarm(0)
        return self

    def __exit__(self, *_):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()

    def _timeout(self, *_):
        raise HandoffError(
            f"waited {LOCK_TIMEOUT_SECONDS}s for the write lock ({self.lock_path}); "
            "another writer is stuck. Retry; the lock frees itself when that process exits"
        )


def write_atomically(path: Path, text: str) -> None:
    if path.exists():  # one-step undo
        path.with_name(f".{path.name}.bak").write_text(path.read_text())
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as out:
        out.write(text)
        out.flush()
        os.fsync(out.fileno())
    if path.exists():
        os.chmod(temp, path.stat().st_mode & 0o777)
    os.replace(temp, path)


def transact(change) -> tuple[Document, Document]:
    path = handoff_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with Locked(path):
        before = read_doc(path)
        after = change(before)
        write_atomically(path, render(after))
    return before, after


# --- CLI -------------------------------------------------------------------------

def stdin_text() -> str:
    if sys.stdin.isatty():
        raise HandoffError("pipe the text on stdin (e.g. a heredoc)")
    text = sys.stdin.read()
    if not text.strip():
        raise HandoffError("stdin was empty")
    return text


def print_entry_line(entry: str) -> None:
    short = status(entry)
    short = short if len(short) <= 100 else short[:99] + "…"
    print(f"{rev(entry)}  {heading(entry)[3:]}\n            status: {short}")


def main(argv: list[str]) -> int:
    today = dt.date.today().isoformat()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="every entry: rev, heading, status")
    p = sub.add_parser("get", help="print one entry; its rev goes to stderr")
    p.add_argument("entry")
    p = sub.add_parser("add", help="new entry at the top; body on stdin")
    p.add_argument("--topic", required=True)
    p.add_argument("--date", default=today)
    p = sub.add_parser("update", help="add a dated **Update** paragraph under the heading")
    p.add_argument("entry")
    p.add_argument("--date", default=today)
    p = sub.add_parser("status", help="replace the **Status:** line")
    p.add_argument("entry")
    p.add_argument("text")
    p = sub.add_parser("replace", help="replace the whole entry; needs --rev")
    p.add_argument("entry")
    p.add_argument("--rev", required=True)
    p = sub.add_parser("remove", help="delete the entry; needs --rev")
    p.add_argument("entry")
    p.add_argument("--rev", required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "list":
            for entry in read_doc(handoff_path()).entries:
                print_entry_line(entry)
            return 0
        if args.command == "get":
            doc = read_doc(handoff_path())
            entry = doc.entries[find(doc, args.entry)]
            print(entry)
            print(f"rev: {rev(entry)}", file=sys.stderr)
            return 0
        changes = {
            "add": lambda: (lambda d: op_add(d, args.topic, args.date, body)),
            "update": lambda: (lambda d: op_update(d, args.entry, args.date, body)),
            "status": lambda: (lambda d: op_status(d, args.entry, args.text)),
            "replace": lambda: (lambda d: op_replace(d, args.entry, args.rev, body)),
            "remove": lambda: (lambda d: op_remove(d, args.entry, args.rev)),
        }
        body = stdin_text() if args.command in ("add", "update", "replace") else ""
        before, after = transact(changes[args.command]())
        if args.command == "remove":
            print(f"removed '{args.entry}'")
        else:
            index = 0 if args.command == "add" else find(before, args.entry)
            print_entry_line(after.entries[index])
        return 0
    except HandoffError as error:
        print(f"handoff: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
