import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "smoke.sh"


CURL_STUB = r"""#!{python}
import json
import os
import sys
from urllib.parse import urlsplit

args = sys.argv[1:]
url = urlsplit(args[-1])
path = url.path + ("?" + url.query if url.query else "")
method = "POST" if "--data-binary" in args else "GET"
payload = None
if method == "POST":
    payload = args[args.index("--data-binary") + 1]
with open(os.environ["REQUESTS"], "a") as stream:
    stream.write(json.dumps({"method": method, "path": path, "payload": payload}) + "\n")
with open(os.environ["RESPONSES"]) as stream:
    responses = json.load(stream)
status, body = responses.get(method + " " + path, responses.get(path, [404, "not found"]))
if "-f" in args and status >= 400:
    sys.exit(22)
sys.stdout.write(body)
"""


DATE_STUB = """#!/bin/sh
count=0
if [ -f "$DATE_CALLS" ]; then read count < "$DATE_CALLS"; fi
count=$((count + 1))
printf '%s\\n' "$count" > "$DATE_CALLS"
if [ "$count" -ge "$DATE_JUMP_AFTER" ]; then printf '281\\n'; else printf '100\\n'; fi
"""


class SmokeScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.temp_path = Path(self.temp.name)
        self.bin_dir = self.temp_path / "bin"
        self.bin_dir.mkdir()
        for name in ("python3", "tr", "sed", "cut", "wc"):
            (self.bin_dir / name).symlink_to(shutil.which(name))
        curl = self.bin_dir / "curl"
        curl.write_text(CURL_STUB.replace("{python}", sys.executable))
        curl.chmod(0o755)
        sleep = self.bin_dir / "sleep"
        sleep.write_text("#!/bin/sh\nexit 0\n")
        sleep.chmod(0o755)
        date = self.bin_dir / "date"
        date.write_text(DATE_STUB)
        date.chmod(0o755)

        self.responses_path = self.temp_path / "responses.json"
        self.requests_path = self.temp_path / "requests.jsonl"
        self.date_calls_path = self.temp_path / "date-calls"
        self.responses = {
            "GET /version.txt": [200, "deadbeef\n"],
            "GET /api/health": [200, json.dumps({"ok": True})],
            "GET /api/metrics": [200, "[]"],
            "GET /api/clusters?limit=1": [200, json.dumps([{"id": 7}])],
            "GET /api/clusters/7": [200, json.dumps({"id": 7, "evidence": [{"kind": "signal"}]})],
            "POST /api/lookup": [202, json.dumps({"id": "lk_0123456789abcdef", "state": "running"})],
            "GET /api/lookup/lk_0123456789abcdef": [200, json.dumps({"id": "lk_0123456789abcdef", "state": "done"})],
        }
        self.base_url = "https://smoke.invalid"

    def run_smoke(self, overrides=None, extra_env=None):
        responses = dict(self.responses)
        responses.update(overrides or {})
        self.responses_path.write_text(json.dumps(responses))
        env = {
            **os.environ,
            "PATH": str(self.bin_dir),
            "RESPONSES": str(self.responses_path),
            "REQUESTS": str(self.requests_path),
            "DATE_CALLS": str(self.date_calls_path),
            "DATE_JUMP_AFTER": "999",
        }
        env.update(extra_env or {})
        return subprocess.run(
            ["/bin/sh", str(SCRIPT), self.base_url],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )

    def requests(self):
        if not self.requests_path.exists():
            return []
        return [json.loads(line) for line in self.requests_path.read_text().splitlines()]

    def test_success_warns_on_empty_metrics_and_polls_lookup(self):
        result = self.run_smoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("WARN /api/metrics is valid but empty", result.stdout)
        self.assertIn("PASS /api/clusters/7 detail JSON evidence_rows=1", result.stdout)
        self.assertIn("PASS lookup job completed (polls=1)", result.stdout)
        posted = [r for r in self.requests() if r["method"] == "POST"]
        self.assertEqual(
            posted,
            [{"method": "POST", "path": "/api/lookup", "payload": '{"url":"https://example.com/"}'}],
        )

    def test_empty_cluster_list_is_a_warning(self):
        result = self.run_smoke({"GET /api/clusters?limit=1": [200, "[]"]})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("WARN /api/clusters?limit=1 returned an empty list", result.stdout)
        self.assertFalse(any(r["path"] == "/api/clusters/7" for r in self.requests()))

    def test_health_failure_is_a_hard_failure(self):
        result = self.run_smoke({"GET /api/health": [200, '{"ok": false}']})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL /api/health did not return JSON with ok=true", result.stdout)
        self.assertIn("RESULT FAIL", result.stdout)

    def test_failed_lookup_is_a_hard_failure(self):
        result = self.run_smoke(
            {"GET /api/lookup/lk_0123456789abcdef": [200, '{"id":"lk_0123456789abcdef","state":"failed"}']}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL lookup job reached failed state", result.stdout)

    def test_lookup_timeout_is_a_hard_failure(self):
        result = self.run_smoke(
            {"GET /api/lookup/lk_0123456789abcdef": [200, '{"id":"lk_0123456789abcdef","state":"running"}']},
            {"DATE_JUMP_AFTER": "3"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL lookup job timed out after 180s", result.stdout)
        polls = [r for r in self.requests() if r["path"] == "/api/lookup/lk_0123456789abcdef"]
        self.assertEqual(len(polls), 1)


if __name__ == "__main__":
    unittest.main()
