"""A claimed compiler check that measured nothing asks admission again by itself.

github_io.py writes verdict=unavailable after publishing an "unavailable" outcome; managed-pr.yml's
notify-unmeasured job sends bfme-unmeasured {number, run} only for a run that claimed its reservation;
admission-unmeasured.yml waits until that run completed plus 150 s and sends bfme-admit-request {number}; admission's
request mode then re-issues the reservation through ledger.reissue_unmeasured (at most twice per commit per main).
"""
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ci/landing/tests"))
from test_land_after_dispatch import finish  # noqa: E402

sys.path.insert(0, str(ROOT / "ci/admission/tests"))
import test_admit as ta  # noqa: E402

WORKFLOWS = ROOT / ".github/workflows"


class Outcome(unittest.TestCase):
    def test_an_unavailable_outcome_is_announced_and_nothing_else_is(self):
        code, output, statuses = finish("infrastructure")
        self.assertEqual(code, 1)
        self.assertEqual(statuses[-1][0], "error")
        self.assertIn("verdict=unavailable", output.splitlines())
        for kind in ("ok", "gate", "regression", "pending"):
            with self.subTest(kind=kind):
                self.assertNotIn("verdict=unavailable", finish(kind)[1])


def job(text, name):
    start = text.index(f"\n  {name}:\n")
    end = text.find("\n  ", start + len(name) + 5)
    while end != -1 and text[end + 3] == " ":
        end = text.find("\n  ", end + 1)
    return text[start:end if end != -1 else len(text)]


class Workflows(unittest.TestCase):
    def test_only_a_claimed_dispatch_started_run_sends_it(self):
        text = (WORKFLOWS / "managed-pr.yml").read_text()
        self.assertIn("unmeasured: ${{ steps.native.outputs.verdict == 'unavailable' && 'yes' || 'no' }}",
                      job(text, "report"))
        block = job(text, "notify-unmeasured")
        for guard in ("needs: [claim, report, fallback-report]", "needs.claim.outputs.allowed == 'yes'",
                      "needs.report.outputs.unmeasured == 'yes'", "github.repository_id == '1464323350'",
                      "github.ref == 'refs/heads/main'", "github.event_name == 'repository_dispatch'",
                      "github.actor_id == '41898282'", '"bfme-unmeasured"', "RUN: ${{ github.run_id }}",
                      '^[1-9][0-9]{0,8}$'):
            self.assertIn(guard, block)
        permissions = block.split("permissions:")[1].split("steps:")[0]
        self.assertEqual([line.strip() for line in permissions.strip().splitlines()], ["contents: write"])
        for forbidden in ("actions/checkout", "download-artifact", "github.event.pull_request"):
            self.assertNotIn(forbidden, block)

    def test_the_waiter_waits_for_completion_plus_quiet_then_asks_once(self):
        text = (WORKFLOWS / "admission-unmeasured.yml").read_text()
        on = "on:" + text.split("\non:")[1].split("\npermissions:")[0]
        self.assertEqual(" ".join(on.split()), "on: repository_dispatch: types: [bfme-unmeasured]")
        self.assertIn("\npermissions: {}\n", text)
        for guard in ("github.repository_id == '1464323350'", "github.ref == 'refs/heads/main'",
                      "github.actor_id == '41898282'", "runs-on: ubuntu-24.04",
                      '^[1-9][0-9]{0,8}$', '^[1-9][0-9]{0,19}$', ".status", "== completed", "sleep 150",
                      '"bfme-admit-request", client_payload: {number: $number}'):
            self.assertIn(guard, text)
        permissions = text.split("    permissions:")[1].split("steps:")[0]
        self.assertEqual(sorted(line.strip() for line in permissions.strip().splitlines()),
                         ["actions: read", "contents: write"])
        self.assertNotIn("actions/checkout", text)
        self.assertEqual(text.count("/dispatches"), 1)
        self.assertGreaterEqual(int(text.split("sleep ")[2].split()[0]), 130)

    def test_admission_answers_the_request(self):
        admission = (WORKFLOWS / "admission.yml").read_text()
        self.assertIn("types: [bfme-admit-request]", admission)


class RequestModeRetries(unittest.TestCase):
    """The bfme-admit-request the waiter sends reaches the same bounded re-issue as the hourly run."""

    def request(self, api):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as stream:
            json.dump({"action": "bfme-admit-request", "client_payload": {"number": 21}}, stream)
        with patch.object(ta.admit, "check_source", lambda *_: None), redirect_stdout(io.StringIO()) as out:
            ta.admit.run(["request"], ta.env(event="repository_dispatch", path=stream.name, total="10", per_author="10"), api=api,
                         config=ta.CONFIG, clock=ta.CLOCK)
        os.unlink(stream.name)
        return out.getvalue()

    def died(self, api, run):
        api.runs = {run: ta.compiler_run(run_id=run)}
        api.statuses.setdefault(ta.HEAD, []).append(
            {"context": ta.admit.BOAT_CONTEXT, "state": "error", "id": api.next_id, "creator": {"id": ta.admit.BOT_ID},
             "description": f"Boat comparison unavailable. Re-run all jobs. (main {ta.MAIN[:12]})"})
        api.next_id += 1

    def test_two_reissues_then_the_plain_hold(self):
        api = ta.LedgerApi(True, [ta.fork_pr()], {21: ["src/a.cpp"]})
        ta.handle(api)
        lines = []
        for attempt, run in enumerate(("900", "901", "902", "903", "904")):
            self.assertEqual(ta.admit.ledger.claim(api, 21, ta.HEAD, ta.MAIN, run, "1", ta.CLOCK, tree=ta.TREE),
                             "claimed")
            self.died(api, run)
            lines.append(self.request(api))
        for idx in range(4):
            self.assertIn("admitted by rule", lines[idx])
        self.assertIn("five times", lines[4])
        self.assertEqual(len([d for d in ta.dispatches(api) if d["event_type"] == "bfme-remeasure"]), 5)
        self.assertEqual(api.statuses[ta.HEAD][-1]["description"], ta.admit.UNMEASURED)


if __name__ == "__main__":
    unittest.main()
