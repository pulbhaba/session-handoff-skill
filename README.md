# Session handoff skill

A small Python command-line tool and agent skill for keeping a shared handoff file safe when multiple sessions or agents may update it.

The tool supports listing and reading entries, adding entries and dated updates, changing status, and replacing or removing entries. Writes queue on a file lock and use an atomic replacement. Replace and remove require a revision from the latest read, which prevents edits based on stale content.

## Install for Codex

Copy this repository's skill files into `~/.codex/skills/session-handoff/` so the directory contains `SKILL.md` and `handoff.py`. The `test_handoff.py` file is optional unless you want to run the tests.

## Use

Run commands from the workspace root. By default, the tool uses `.claude/session-handoff.md` in the current directory. Set `HANDOFF_FILE` to use a different path.

```sh
python3 ~/.codex/skills/session-handoff/handoff.py list
python3 ~/.codex/skills/session-handoff/handoff.py get "topic words"
```

See [SKILL.md](SKILL.md) for all commands and the write-safety rules.

Requires Python 3.9+ on macOS or Linux. The tool uses only the Python standard library.
