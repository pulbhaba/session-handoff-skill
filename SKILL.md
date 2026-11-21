---
name: session-handoff
description: Read and write a shared session handoff file safely when several sessions, agents, or people use it at once. Supports listing entries, reading one, adding an entry or dated update, changing a status, and replacing or removing an entry. Writes are locked and stale edits are refused. Use when reading or changing the handoff, sharing information with another session, or continuing work from another session.
argument-hint: "list | get <entry> | add --topic <t> | update <entry> | status <entry> <text> | replace <entry> --rev <r> | remove <entry> --rev <r>"
---

# Session handoff: safe reads and writes

The session handoff file passes findings between sessions. Several sessions and agents may write to it, so **every change goes through `handoff.py`**. Never change it with an editor or a direct file-writing command: those skip the lock and can silently overwrite another session's entry. Reading the file directly is fine.

The default file is `.claude/session-handoff.md` under the current workspace root. Run commands from that root, or set `HANDOFF_FILE` to use another path. Requires Python 3.9+ and the standard library; locking uses `fcntl` on macOS and Linux.

## Commands

From the workspace root (replace the script path with where you installed this skill):

```bash
python3 /path/to/session-handoff/handoff.py list  # rev, heading and status, newest first
python3 /path/to/session-handoff/handoff.py get "topic words"  # prints "rev: <rev>" on stderr

python3 /path/to/session-handoff/handoff.py add --topic "Deployment investigation" <<'EOF'
**Status:** open, waiting on the user.

**Where:** repository, branch, and relevant paths.

Findings, open questions, and next step.
EOF

python3 /path/to/session-handoff/handoff.py update "Deployment investigation" <<'EOF'
New finding or progress note.
EOF
python3 /path/to/session-handoff/handoff.py status "Deployment investigation" "complete"

python3 /path/to/session-handoff/handoff.py replace "Deployment investigation" --rev 1a2b3c4d5e <<'EOF'
...the whole new body (keeps the heading; start with "## " to change the heading too)...
EOF
python3 /path/to/session-handoff/handoff.py remove "Deployment investigation" --rev 1a2b3c4d5e
```

- `<entry>` is any text found in exactly one entry heading: a ticket key, a date or a word from the topic. More than one match is an error that lists them.
- `add` dates the heading today (`--date` to change it) and refuses a body without a `**Status:**` line.
- `update` puts `**Update <date>:** <text>` right under the heading, newest first. Prefer it to `replace` for adding news to an entry: it can't lose anything.
- `status` replaces only the text of the entry's `**Status:**` line.
- `replace` and `remove` need the `rev` from your latest `get` or `list`. If anyone changed the entry since, they fail with "entry changed since you read it": run `get` again, merge your change into the new text, and retry.
- Exit code 0 on success, 1 with a `handoff:` message on stderr otherwise.

## How it stays safe

1. **Write lock, with writers queued.** Each write takes an exclusive `flock` on `.claude/.session-handoff.md.lock`, then reads the file, changes one entry and writes it back while still holding the lock. Writers that arrive meanwhile wait in the kernel's queue for that lock (up to 30 s, `HANDOFF_LOCK_TIMEOUT`), so no write is lost. A crashed writer's lock is freed by the OS when its process ends, so there are no stale lock files to clean up.
2. **Atomic swap.** The new file is written to a temp file in the same folder, synced, and renamed over the old one, so a reader never sees a half-written file and needs no lock.
3. **Revision check for read-then-write.** An agent reads in one tool call and writes in a later one, and a lock can't span the gap. So `replace` and `remove`, the two commands that can discard text, check the entry's `rev` (a hash of its text) first. `add`, `update` and `status` only add or change a known part, so they need none.
4. **One-step undo.** The file as it was before the last write is kept in `.claude/.session-handoff.md.bak`.

The order of two writers who wait at the same moment is not guaranteed. It doesn't need to be: each command touches one entry, and two adds made at the same moment are both "newest".

## Using it from anywhere

- **Other repos:** install the skill in the agent's skills directory or link it into the repository using that agent's skill discovery convention. Run it from the workspace root so it uses that workspace's `.claude/session-handoff.md`.
- **Subagents and other tools:** any process that can run `python3` uses the same commands. Give a subagent the path to this file and the commands it needs.
- **Another file:** `HANDOFF_FILE=/path/to/file.md` points the script at a different file (the tests use this).

## Tests

```bash
python3 -I /path/to/session-handoff/test_handoff.py
```

They cover the byte-for-byte round trip of the real file, `##` lines inside code fences, every command, stale revs, ambiguous matches, and 40 writers at once (20 adds and 20 updates in parallel, nothing lost). Run them after changing the script or the entry format.

## Notes

- The format it expects: a preamble, then entries that start with `## <date>: <topic>`, separated by `---` lines, each with a `**Status:**` line. If you change the format, update `parse`/`render` and the tests together.
