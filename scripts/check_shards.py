#!/usr/bin/env python3
"""Prove the CI shards actually cover every test in the backend.

The shards in .github/shards.json exist so the suite finishes in the time one
shard takes rather than the half hour it takes end to end. That trade is only
sound if the shards between them run *everything*.

Nothing about the sharded layout notices an app that no shard names. Adding
apps/loyalty with a full test suite and forgetting to add it here would produce
a completely green CI run that never executed a line of it - which is a worse
failure than a red tick, because it looks like proof.

So this asserts the two directions that can go wrong:

  * every app with tests is claimed by some shard
  * every path a shard names actually exists

Run by the `checks` job on every push. Exits non-zero with the specific app
named, because "shards are out of date" is not something anybody can act on.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SHARD_FILE = REPO_ROOT / ".github" / "shards.json"
BACKEND = REPO_ROOT / "backend"


def apps_with_tests() -> set[str]:
    """Every app directory holding at least one test module."""
    found = set()
    for tests_dir in (BACKEND / "apps").glob("*/tests"):
        if any(tests_dir.glob("test_*.py")):
            found.add(f"apps/{tests_dir.parent.name}")
    # A few apps keep tests in a single module rather than a package.
    for tests_module in (BACKEND / "apps").glob("*/tests.py"):
        found.add(f"apps/{tests_module.parent.name}")
    return found


def sharded_paths() -> tuple[set[str], list[str]]:
    """What the shard file claims, and the raw list for duplicate detection."""
    shards = json.loads(SHARD_FILE.read_text())
    claimed: list[str] = []
    for shard in shards:
        claimed.extend(shard["paths"].split())
    return set(claimed), claimed


def main() -> int:
    actual = apps_with_tests()
    claimed, claimed_list = sharded_paths()

    problems: list[str] = []

    unclaimed = sorted(actual - claimed)
    if unclaimed:
        problems.append(
            "These apps have tests that no CI shard runs:\n"
            + "\n".join(f"  {app}" for app in unclaimed)
            + "\n\nAdd each to a shard in .github/shards.json. Until then CI is\n"
            "green without having run them."
        )

    missing = sorted(claimed - actual)
    if missing:
        problems.append(
            "These shard entries name paths with no tests:\n"
            + "\n".join(f"  {path}" for path in missing)
            + "\n\nEither the app was removed or the path is misspelled. A shard\n"
            "pointing at nothing silently shrinks what CI covers."
        )

    duplicates = sorted({p for p in claimed_list if claimed_list.count(p) > 1})
    if duplicates:
        problems.append(
            "These paths appear in more than one shard:\n"
            + "\n".join(f"  {path}" for path in duplicates)
            + "\n\nHarmless to correctness, but it runs them twice and unbalances\n"
            "the shards."
        )

    if problems:
        print("\n\n".join(problems), file=sys.stderr)
        return 1

    print(f"Shards cover all {len(actual)} apps with tests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
