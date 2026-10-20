"""First-time-contributor approval: admit exactly the held-then-approved attempt 2."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_ci_contract import admitted

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("admission", ROOT / "tools/ci_approval_admission.py")
admission = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(admission)

HEAD = "d" * 40
ORDINARY = {"hassfest", "hacs", "ruff", "pytest", "yamllint"}


def _wf(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def _env(**over):
    env = {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_RUN_ID": "42",
           "GITHUB_REPOSITORY": "dkpnw/ha-mxz-coordinator", "HEAD_SHA": HEAD}
    env.update(over)
    return env


def _fetcher(first=None, jobs=0, error=False):
    held = {"id": 42, "run_attempt": 1, "event": "pull_request", "status": "completed",
            "conclusion": "action_required", "head_sha": HEAD}
    held.update(first or {})

    def fetch(path):
        if error:
            raise OSError("invented API failure")
        assert path.startswith("/repos/dkpnw/ha-mxz-coordinator/actions/runs/42/attempts/1")
        return {"total_count": jobs} if "/jobs" in path else held
    return fetch


def test_held_then_approved_attempt_is_admitted():
    assert admission.decide(_env(), _fetcher()) == (True, "admitted")


@pytest.mark.parametrize("env,fetch,reason", [
    (_env(GITHUB_EVENT_NAME="push"), _fetcher(), "event"),
    (_env(GITHUB_EVENT_NAME="workflow_dispatch"), _fetcher(), "event"),
    (_env(GITHUB_RUN_ATTEMPT="1"), _fetcher(), "attempt"),
    (_env(GITHUB_RUN_ATTEMPT="3"), _fetcher(), "attempt"),
    (_env(GITHUB_RUN_ID="x"), _fetcher(), "run"),
    (_env(HEAD_SHA="bad"), _fetcher(), "head"),
    (_env(), _fetcher(jobs=6), "never-ran"),  # an ordinary re-run of an executed attempt 1
    (_env(), _fetcher({"conclusion": "failure"}), "held"),
    (_env(), _fetcher({"conclusion": "success"}), "held"),
    (_env(), _fetcher({"status": "in_progress"}), "status"),
    (_env(), _fetcher({"event": "push"}), "event"),
    (_env(), _fetcher({"head_sha": "e" * 40}), "head"),
    (_env(), _fetcher({"id": 41}), "id"),
    (_env(), _fetcher(error=True), "api"),
], ids=["push", "manual", "attempt-1", "attempt-3", "bad-run", "bad-head", "rerun-of-executed",
        "rerun-of-failure", "rerun-of-success", "unfinished", "other-event", "other-head", "other-run", "api"])
def test_everything_else_is_refused(env, fetch, reason):
    assert admission.decide(env, fetch) == (False, reason)


def test_main_writes_output_and_fails_visibly(tmp_path, monkeypatch):
    out = tmp_path / "out"
    for key, value in _env(GITHUB_RUN_ATTEMPT="3").items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert admission.main() == 1
    assert out.read_text() == "admitted=false\n"


PR = ("pull_request", "refs/pull/28/merge", "fix/feature", "main")


@pytest.mark.parametrize("event,ref,head,base,attempt,approval,ordinary", [
    (*PR, 2, "true", True),
    (*PR, 2, "false", False),
    (*PR, 2, "", False),
    (*PR, 3, "true", False),
    (*PR, 1, "", True),
    ("push", "refs/heads/feature", "", "", 2, "true", False),
    ("workflow_dispatch", "refs/heads/feature", "", "", 2, "true", False),
    ("pull_request", "refs/pull/28/merge", "ci/issue25-discriminator", "main", 2, "true", False),
    ("pull_request", "refs/pull/28/merge", "", "main", 2, "true", False),
], ids=["approved", "approval-refused", "approval-skipped", "attempt-3", "attempt-1", "push-rerun",
        "manual-rerun", "diagnostic-head", "missing-head"])
def test_job_gates_with_approval(event, ref, head, base, attempt, approval, ordinary):
    wf = _wf("ci.yml")
    context = {"event_name": event, "ref": ref, "head_ref": head, "base_ref": base, "run_attempt": attempt}
    assert admitted(wf["jobs"]["approval"]["if"], context) is (attempt == 2 and event == "pull_request")
    selected = {n for n, job in wf["jobs"].items() if n != "approval" and admitted(job["if"], context, approval)}
    assert selected == (ORDINARY if ordinary else {"issue25"})


def _guard(run, **over):
    env = {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/pull/28/merge", "GITHUB_HEAD_REF": "fix/feature",
           "GITHUB_BASE_REF": "main", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_SHA": "a" * 40,
           "REPOSITORY_PRIVATE": "false", "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_ARCH": "X64",
           "APPROVAL_ADMITTED": "true"}
    env.update(over)
    return subprocess.run(["bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", run], env=env,
                          check=False, capture_output=True, text=True, timeout=5)


@pytest.mark.parametrize("over,ok", [
    ({}, True),
    ({"APPROVAL_ADMITTED": "false"}, False),
    ({"APPROVAL_ADMITTED": ""}, False),
    ({"GITHUB_RUN_ATTEMPT": "3"}, False),
    ({"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/feature"}, False),
    ({"GITHUB_RUN_ATTEMPT": "1", "APPROVAL_ADMITTED": ""}, True),
], ids=["approved", "refused", "skipped", "attempt-3", "push", "attempt-1"])
def test_ordinary_step_guards_follow_the_approval(over, ok):
    for name in sorted(ORDINARY):
        result = _guard(_wf("ci.yml")["jobs"][name]["steps"][0]["run"], **over)
        assert (result.returncode == 0) is ok, (name, result.stderr)
        if not ok:
            assert result.stderr.strip() == "ISSUE25_REFUSED=attempt"


def test_diagnostic_guard_still_refuses_any_pr():
    result = _guard(_wf("ci.yml")["jobs"]["issue25"]["steps"][0]["run"])
    assert result.returncode != 0 and result.stderr.strip() == "ISSUE25_REFUSED=event"


def test_ha202610_guard_follows_the_approval():
    run = _wf("ci-ha2026.10.yml")["jobs"]["exact-ha202610"]["steps"][0]["run"]
    run = run.replace(". /etc/os-release", "ID=ubuntu VERSION_ID=26.04")
    extra = {"ImageOS": "ubuntu26", "ImageVersion": "invented"}
    assert _guard(run, **extra).returncode == 0
    refused = _guard(run, APPROVAL_ADMITTED="false", **extra)
    assert refused.returncode != 0 and refused.stderr.strip() == "HA202610_REFUSED=attempt"
    script = (ROOT / "tools/ci-ha202610.sh").read_text()
    assert 'test "${APPROVAL_ADMITTED:-}" = true' in script


@pytest.mark.parametrize("change,error", [
    ("widen", "job credential override"),
    ("write", "job credential override"),
    ("ordinary-perms", "job credential override"),
    ("drop-needs", "approval dependency"),
])
def test_approval_cannot_be_widened_or_bypassed(change, error):
    from tests.test_ci_contract import violations

    wf = _wf("ci.yml")
    assert violations(wf) == []
    if change == "widen":
        wf["jobs"]["approval"]["permissions"]["pull-requests"] = "read"
    elif change == "write":
        wf["jobs"]["approval"]["permissions"]["actions"] = "write"
    elif change == "ordinary-perms":
        wf["jobs"]["pytest"]["permissions"] = {"contents": "read", "actions": "read"}
    else:
        del wf["jobs"]["ruff"]["needs"]
    assert error in violations(wf)
