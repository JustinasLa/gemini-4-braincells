import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
METRIC_ROW = {"dataset": "organic_live", "split": "heldout", "details": {"detector_version": "v9"}}


@pytest.fixture(params=[False, True], ids=["python", "jq"])
def run_check(tmp_path, request):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("python3", "tr", "date", "grep"):
        executable = shutil.which(name)
        assert executable
        (binaries / name).symlink_to(executable)
    if request.param:
        jq = shutil.which("jq")
        if not jq:
            pytest.skip("jq unavailable")
        (binaries / "jq").symlink_to(jq)
    curl = binaries / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from urllib.parse import urlsplit\n"
        "with open(os.environ['RESPONSES']) as stream: responses = json.load(stream)\n"
        "url = urlsplit(sys.argv[-1])\n"
        "path = url.path + ('?' + url.query if url.query else '')\n"
        "with open(os.environ['REQUESTS'], 'a') as stream: stream.write(path + '\\n')\n"
        "status, body = responses.get(path, [404, 'not found'])\n"
        "if '-fsS' in sys.argv and status >= 400: sys.exit(22)\n"
        "print(body, end='')\n"
        "if '-w' in sys.argv: print('\\n' + str(status), end='')\n"
    )
    curl.chmod(0o755)
    responses_file = tmp_path / "responses.json"
    requests_file = tmp_path / "requests.txt"

    def run(overrides=None):
        responses = {
            "/api/health": [200, json.dumps({"ok": True, "ingest_lag_s": 1})],
            "/api/clusters?limit=1": [200, "[]"],
            "/api/clusters?limit=50": [200, json.dumps([{"id": ident} for ident in range(50)])],
            "/api/metrics": [200, json.dumps([METRIC_ROW])],
            "/": [200, "<div>Trackdown</div>"],
            "/trackdown/": [200, '<html><script src="app.js"></script></html>'],
            "/version.txt": [200, "test-version"],
        }
        for ident in range(5):
            responses[f"/api/clusters/{ident}"] = [200, '{"evidence": [{"kind": "media"}]}']
        responses.update(overrides or {})
        responses_file.write_text(json.dumps(responses))
        result = subprocess.run(
            [shutil.which("bash"), str(ROOT / "deploy/check.sh"), "https://test.invalid"],
            env={
                **os.environ,
                "PATH": str(binaries),
                "RESPONSES": str(responses_file),
                "REQUESTS": str(requests_file),
                "MAX_LAG_S": "300",
            },
            text=True,
            capture_output=True,
            timeout=10,
        )
        return result, requests_file.read_text().splitlines()

    return run


def test_healthy_site_reports_sampled_media_coverage(run_check):
    result, requests = run_check()
    assert result.returncode == 0, result.stderr
    assert "media evidence clusters=5/5 checked (latest=50; unavailable=0" in result.stdout
    assert not any(line.startswith("WARN") for line in result.stdout.splitlines())
    assert " OK " in result.stdout
    assert "/api/clusters?limit=50" in requests
    assert len([path for path in requests if path.startswith("/api/clusters/")]) == 5


def test_no_media_is_a_warning_not_a_failure(run_check):
    result, _ = run_check(
        {f"/api/clusters/{ident}": [200, '{"evidence": []}'] for ident in range(5)}
    )
    assert result.returncode == 0
    assert "media evidence clusters=0/5 checked" in result.stdout
    assert "WARN media evidence absent" in result.stdout
    assert " OK " in result.stdout


@pytest.mark.parametrize("status,body", [(200, "[]"), (200, "not json"), (503, "unavailable")])
def test_empty_or_unavailable_media_list_is_soft(run_check, status, body):
    result, _ = run_check({"/api/clusters?limit=50": [status, body]})
    assert result.returncode == 0
    assert "WARN media evidence" in result.stdout and " OK " in result.stdout


def test_unreadable_details_are_not_counted_as_no_media(run_check):
    result, _ = run_check({"/api/clusters/0": [404, "gone"], "/api/clusters/1": [200, "not json"]})
    assert result.returncode == 0
    assert "media evidence clusters=3/3 checked (latest=50; unavailable=2" in result.stdout
    assert "WARN media evidence" in result.stdout


@pytest.mark.parametrize(
    "status,body", [(200, '{"ok": true}'), (302, "<div>Redirect</div>"), (404, "<div>Error</div>")]
)
def test_trackdown_requires_200_and_html(run_check, status, body):
    result, _ = run_check({"/trackdown/": [status, body]})
    assert result.returncode == 1
    assert " FAIL " in result.stdout and "/trackdown/ must return HTTP 200" in result.stdout


def test_existing_health_failure_is_still_hard(run_check):
    result, _ = run_check({"/api/health": [200, '{"ok": true, "ingest_lag_s": 500}']})
    assert result.returncode == 1 and "health not ok" in result.stdout


def test_empty_metrics_is_a_warning_not_a_failure(run_check):
    result, _ = run_check({"/api/metrics": [200, "[]"]})
    assert result.returncode == 0, result.stderr
    assert "WARN /api/metrics is empty" in result.stdout and " OK " in result.stdout


@pytest.mark.parametrize(("deployed", "warns"), [("v9", False), ("v10", True)])
def test_metrics_must_name_the_deployed_detector(run_check, tmp_path, deployed, warns):
    git = tmp_path / "bin" / "git"  # the deployed commit's signals/config.py
    git.write_text(f"#!/bin/sh\necho 'VERSION = \"{deployed}\"'\n")
    git.chmod(0o755)
    result, _ = run_check()
    assert result.returncode == 0, result.stderr
    assert ("WARN eval rows are for detector ['v9'], the site runs v10" in result.stdout) is warns
    assert ("INFO metrics rows=1 replay detector=['v9'] deployed=v9" in result.stdout) is not warns


def test_wheel_ships_every_backend_package():
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    shipped = set(config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"])
    packages = {p.parent.name for p in ROOT.glob("*/__init__.py")} - {"tests"}
    assert packages <= shipped, sorted(packages - shipped)
