"""Admit attempt 2 of a PR run only when attempt 1 was a first-time-contributor approval hold.

GitHub never executes the held run itself: attempt 1 completes as ``action_required``
with zero jobs, and approving it runs everything as attempt 2. Every ordinary job
still refuses any other attempt 2 (an ordinary re-run) and every attempt 3+.

Writes ``admitted=true|false`` to $GITHUB_OUTPUT and exits non-zero when refused,
so a refusal is a visible failure, never a silent skip. Read-only API calls only.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any


def decide(env: Mapping[str, str], fetch: Callable[[str], Any]) -> tuple[bool, str]:
    if env.get("GITHUB_EVENT_NAME") != "pull_request":
        return False, "event"
    if env.get("GITHUB_RUN_ATTEMPT") != "2":
        return False, "attempt"
    run_id = env.get("GITHUB_RUN_ID", "")
    repo = env.get("GITHUB_REPOSITORY", "")
    head = env.get("HEAD_SHA", "")
    if not re.fullmatch(r"[0-9]+", run_id) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        return False, "run"
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        return False, "head"
    base = f"/repos/{repo}/actions/runs/{run_id}/attempts/1"
    try:
        first = fetch(base)
        jobs = fetch(base + "/jobs?per_page=1")
    except (OSError, ValueError):  # HTTP, network or JSON failure refuses
        return False, "api"
    checks = {
        "id": first.get("id") == int(run_id),
        "attempt": first.get("run_attempt") == 1,
        "event": first.get("event") == "pull_request",
        "status": first.get("status") == "completed",
        "held": first.get("conclusion") == "action_required",
        "head": first.get("head_sha") == head,
        "never-ran": jobs.get("total_count") == 0,
    }
    failed = [name for name, ok in checks.items() if not ok]
    return (not failed), ("admitted" if not failed else failed[0])


def _fetch(path: str) -> Any:
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    request = urllib.request.Request(api + path, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + os.environ["GH_TOKEN"],
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def main() -> int:
    admitted, reason = decide(os.environ, _fetch)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
        out.write(f"admitted={'true' if admitted else 'false'}\n")
    print(f"APPROVAL_ADMISSION={reason}")
    return 0 if admitted else 1


if __name__ == "__main__":
    sys.exit(main())
