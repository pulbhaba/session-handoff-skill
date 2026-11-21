"""Tests for handoff.py. Run: python3 -I test_handoff.py

Each test works on a temporary copy through HANDOFF_FILE.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import handoff  # noqa: E402

SCRIPT = HERE / "handoff.py"
SAMPLE = """# Session handoff

Intro.

---

## 2026-11-02: second topic

**Status:** open.

Body with a fence:

```md
## not a heading
```

---

## 2026-11-01: first topic

**Status:** waiting on the user.

---
"""


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.file = self.dir / "session-handoff.md"
        self.file.write_text(SAMPLE)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_cli(self, *args, stdin=None):
        env = {**os.environ, "HANDOFF_FILE": str(self.file)}
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), *args],
            input=stdin, capture_output=True, text=True, env=env,
        )

    def test_round_trips_the_sample_document(self):
        self.assertEqual(handoff.render(handoff.parse(SAMPLE)), SAMPLE)

    def test_heading_inside_a_code_fence_is_not_an_entry(self):
        self.assertEqual(len(handoff.parse(SAMPLE).entries), 2)

    def test_add_puts_the_entry_on_top_and_needs_a_status(self):
        self.assertEqual(self.run_cli("add", "--topic", "x", stdin="no status").returncode, 1)
        done = self.run_cli("add", "--topic", "new", "--date", "2026-11-03", stdin="**Status:** open.\n")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(handoff.heading(handoff.parse(self.file.read_text()).entries[0]),
                         "## 2026-11-03: new")

    def test_update_and_status(self):
        self.run_cli("update", "first topic", "--date", "2026-11-04", stdin="it moved")
        self.run_cli("status", "first topic", "done")
        entry = handoff.parse(self.file.read_text()).entries[1]
        self.assertIn("**Update 2026-11-04:** it moved", entry)
        self.assertEqual(handoff.status(entry), "done")

    def test_replace_and_remove_refuse_a_stale_rev(self):
        rev = self.run_cli("get", "first topic").stderr.split("rev: ")[1].strip()
        self.run_cli("status", "first topic", "changed by another session")
        stale = self.run_cli("replace", "first topic", "--rev", rev, stdin="**Status:** mine")
        self.assertEqual(stale.returncode, 1)
        self.assertIn("changed since you read it", stale.stderr)
        self.assertEqual(self.run_cli("remove", "first topic", "--rev", rev).returncode, 1)
        fresh = self.run_cli("get", "first topic").stderr.split("rev: ")[1].strip()
        self.assertEqual(self.run_cli("remove", "first topic", "--rev", fresh).returncode, 0)
        self.assertEqual(len(handoff.parse(self.file.read_text()).entries), 1)

    def test_ambiguous_entry_is_refused(self):
        result = self.run_cli("status", "topic", "x")
        self.assertEqual(result.returncode, 1)
        self.assertIn("matches 2 entries", result.stderr)

    def test_parallel_writers_lose_nothing(self):
        def add(n):
            return self.run_cli("add", "--topic", f"writer {n}", stdin=f"**Status:** open {n}.")

        def update(n):
            return self.run_cli("update", "first topic", stdin=f"note {n}")

        with ThreadPoolExecutor(max_workers=40) as pool:
            results = list(pool.map(add, range(20))) + list(pool.map(update, range(20)))
        self.assertTrue(all(r.returncode == 0 for r in results), [r.stderr for r in results])
        text = self.file.read_text()
        for n in range(20):
            self.assertIn(f"## {handoff.dt.date.today()}: writer {n}\n", text)
            self.assertIn(f":** note {n}\n", text)
        self.assertEqual(len(handoff.parse(text).entries), 22)


if __name__ == "__main__":
    unittest.main()
