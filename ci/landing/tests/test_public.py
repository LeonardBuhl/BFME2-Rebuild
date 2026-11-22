"""Public-repository cases: pull requests from forks, and the merge-queue mode.

fixtures/fork-shapes.json holds two real open fork pull requests from a public repository (cli/cli, read on
2026-11-20), trimmed to the fields the lander reads: author_association NONE and CONTRIBUTOR, head.repo.fork
true, head.repo.id different from the base. Each test grafts that real fork shape onto pull request 2's captured
payload, so the checks and receipts stay the real ones.
Run from the repository root:  python3 -m unittest discover -s ci/landing/tests -v
"""
import json
from pathlib import Path
import tempfile
import unittest

from test_lander import (CONFIG, FakeApi, LIVE_MAIN, MEASURED, NEW, PR2, TREE, env, lander, put_receipts, quiet,
                         receipts_match_checkout)

SHAPES = json.loads((Path(__file__).parent / "fixtures/fork-shapes.json").read_text())
NO_EXCEPTIONS = CONFIG | {"exceptions": set()}
NODE = "PR_kwFORKPULLREQUEST2"


def as_fork(association="NONE", labels=(), ref="canary/chosen-by-the-fork-author"):
    shape = SHAPES[association]

    def edit(fx):
        pr = fx["pulls/2"]
        pr["head"]["repo"] = dict(shape["head"]["repo"])
        pr["head"]["ref"] = ref
        pr["author_association"] = shape["author_association"]
        pr["user"] = {"id": 424242, "login": "a-stranger"}
        pr["labels"] = [{"name": name} for name in labels]
        pr["node_id"] = NODE
    return edit


def landing_statuses(api, head=PR2):
    return [w[2] for w in api.writes if w[1] == f"statuses/{head}"]


class ForkShape(unittest.TestCase):
    def test_captured_shapes_are_forks_of_another_repository(self):
        for association, pr in SHAPES.items():
            self.assertTrue(pr["head"]["repo"]["fork"])
            self.assertNotEqual(pr["head"]["repo"]["id"], pr["base"]["repo"]["id"])
            self.assertEqual(pr["author_association"], association)


class Forks(unittest.TestCase):
    def test_unadmitted_fork_is_held_with_a_next_step(self):
        for association in SHAPES:
            decision = lander.evaluate(FakeApi(live=False, main=MEASURED, edit=as_fork(association)), 2, MEASURED,
                                       NO_EXCEPTIONS)
            first = decision["reasons"][0]
            self.assertEqual((decision["stage"], first.code), ("hold", "awaiting-admission"))
            self.assertIn("boat-ok", first.found)
            self.assertIn("you do not need to do anything", first.found)
            self.assertNotIn("refused-branch", [item.code for item in decision["reasons"]])

    def test_unadmitted_stale_fork_is_not_remeasured(self):
        api = FakeApi(live=True, main=LIVE_MAIN, edit=as_fork())
        with tempfile.TemporaryDirectory() as tmp:
            code, out = quiet(lander.run, ["select", tmp + "/w"], env(main=LIVE_MAIN, mode="live", pr="2"),
                              api=api, config=NO_EXCEPTIONS)
        self.assertEqual(code, 0)
        self.assertNotIn("dispatches", [w[1] for w in api.writes])
        self.assertEqual(landing_statuses(api)[-1]["state"], "pending")
        self.assertTrue(landing_statuses(api)[-1]["description"].startswith("Held: awaiting-admission."))
        self.assertIn("Nothing to land", out)

    def test_fork_branch_name_decides_nothing(self):
        for ref in ("canary/x", "corpus/base-1", "ci-corpus/y", "main"):
            api = FakeApi(live=False, main=MEASURED, edit=as_fork(labels=("boat-ok",), ref=ref))
            decision = lander.evaluate(api, 2, MEASURED, NO_EXCEPTIONS)
            self.assertEqual((decision["stage"], decision["reasons"]), ("land", []), ref)

    def test_same_repository_rehearsal_branch_is_still_refused(self):
        decision = lander.evaluate(FakeApi(live=False, main=MEASURED), 2, MEASURED, NO_EXCEPTIONS)
        self.assertEqual(decision["reasons"][0].code, "refused-branch")

    def test_admitted_stale_fork_goes_through_admission_not_the_label(self):
        # Audit 1.6(c): a label is not bound to a commit. Switched off, the outsider waits for a maintainer;
        # switched on, the admission workflow decides by rule for this exact head and main.
        for auto, expected in ((False, None), (True, "bfme-admit-request")):
            api = FakeApi(live=True, main=LIVE_MAIN, edit=as_fork("CONTRIBUTOR", labels=("boat-ok",)))
            ticks = iter(range(0, 10**7, 900))
            with tempfile.TemporaryDirectory() as tmp:
                quiet(lander.run, ["select", tmp + "/w"], env(main=LIVE_MAIN, mode="live", pr="2"), api=api,
                      config=NO_EXCEPTIONS | {"auto_admission": auto}, clock=lambda: next(ticks),
                      sleep=lambda s: None)
            sent = [w[2]["event_type"] for w in api.writes if w[1] == "dispatches"]
            self.assertEqual(sent[:1], [expected] if expected else [], auto)

    def test_new_commit_after_admission_names_readmission(self):
        # boat-ok stays on the pull request, but the compiler check does not run a contributor's new commit by itself.
        def pushed(fx):
            as_fork("CONTRIBUTOR", labels=("boat-ok",))(fx)
            new = "8" * 40
            fx["pulls/2"]["head"]["sha"] = new
            fx[f"commits/{new}/statuses?per_page=100&page=1"] = [
                item for item in fx[f"commits/{PR2}/statuses?per_page=100&page=1"]
                if item["context"] == lander.RULES]
        decision = lander.evaluate(FakeApi(live=False, main=MEASURED, edit=pushed), 2, MEASURED, NO_EXCEPTIONS)
        first = decision["reasons"][0]
        self.assertEqual(first.code, "awaiting-readmission")
        self.assertIn("remove and add the boat-ok label", first.expected)
        self.assertIn("you do not need to do anything", first.found)
        self.assertEqual(decision["stage"], "hold")

    def test_member_fork_without_status_is_plain_no_status(self):
        def member(fx):
            as_fork()(fx)
            fx["pulls/2"]["author_association"] = "MEMBER"
            fx[f"commits/{PR2}/statuses?per_page=100&page=1"] = [
                item for item in fx[f"commits/{PR2}/statuses?per_page=100&page=1"]
                if item["context"] != lander.BOAT]
        decision = lander.evaluate(FakeApi(live=False, main=MEASURED, edit=member), 2, MEASURED, NO_EXCEPTIONS)
        self.assertEqual(decision["reasons"][0].code, "no-status")

    def test_commit_admitted_by_rule_is_eligible_without_a_label(self):
        def ruled(fx, creator=lander.lr.BOT_ID, head=PR2):
            as_fork()(fx)
            fx[f"commits/{head}/statuses?per_page=100&page=1"].insert(0, {
                "id": 10**12, "context": "BFME II / Admission", "state": "success",
                "description": "Admitted by rule: source files only", "creator": {"id": creator}})
        decision = lander.evaluate(FakeApi(live=False, main=MEASURED, edit=ruled), 2, MEASURED, NO_EXCEPTIONS)
        self.assertEqual((decision["stage"], decision["reasons"]), ("land", []))
        forged = lander.evaluate(FakeApi(live=False, main=MEASURED, edit=lambda fx: ruled(fx, creator=1760627)),
                                 2, MEASURED, NO_EXCEPTIONS)
        self.assertEqual(forged["reasons"][0].code, "awaiting-admission")

    def test_admission_of_an_older_commit_does_not_carry_over(self):
        def older(fx):
            as_fork()(fx)
            old = "5" * 40
            fx[f"commits/{old}/statuses?per_page=100&page=1"] = [{
                "id": 1, "context": "BFME II / Admission", "state": "success",
                "description": "Admitted by rule: source files only", "creator": {"id": lander.lr.BOT_ID}}]
        decision = lander.evaluate(FakeApi(live=False, main=MEASURED, edit=older), 2, MEASURED, NO_EXCEPTIONS)
        self.assertEqual(decision["reasons"][0].code, "awaiting-admission")

    def test_deleted_fork_is_held(self):
        def gone(fx):
            as_fork(labels=("boat-ok",))(fx)
            fx["pulls/2"]["head"]["repo"] = None
        decision = lander.evaluate(FakeApi(live=False, main=MEASURED, edit=gone), 2, MEASURED, NO_EXCEPTIONS)
        self.assertEqual(decision["reasons"][0].code, "head-repository-missing")


def admission(fx, state="success", description=None, creator=None):
    fx[f"commits/{PR2}/statuses?per_page=100&page=1"].insert(0, {
        "id": 10**12, "context": "BFME II / Admission", "state": state,
        "description": description or f"Admitted by rule: source files only (main {LIVE_MAIN[:12]})",
        "creator": {"id": creator or lander.lr.BOT_ID}})


class RuleAdmittedRemeasure(unittest.TestCase):
    """Re-measuring a fork admitted by rule goes through the admission workflow, which reserves first."""

    def test_asks_admission_instead_of_dispatching_the_compiler(self):
        api = FakeApi(live=True, main=LIVE_MAIN, edit=lambda fx: (as_fork()(fx), admission(fx)))
        ticks = iter(range(0, 10**7, 900))
        with tempfile.TemporaryDirectory() as tmp:
            quiet(lander.run, ["select", tmp + "/w"], env(main=LIVE_MAIN, mode="live", pr="2"), api=api,
                  config=NO_EXCEPTIONS | {"auto_admission": True}, clock=lambda: next(ticks), sleep=lambda s: None)
        self.assertEqual(api.writes[0], ("POST", "dispatches", {"event_type": "bfme-admit-request",
                                                                "client_payload": {"number": 2}}))
        self.assertNotIn("bfme-remeasure", [w[2].get("event_type") for w in api.writes if w[1] == "dispatches"])

    def run_stale(self, edit, auto):
        api = FakeApi(live=True, main=LIVE_MAIN, edit=edit)
        ticks = iter(range(0, 10**7, 900))
        with tempfile.TemporaryDirectory() as tmp:
            _, out = quiet(lander.run, ["select", tmp + "/w"], env(main=LIVE_MAIN, mode="live", pr="2"), api=api,
                           config=NO_EXCEPTIONS | {"auto_admission": auto}, clock=lambda: next(ticks),
                           sleep=lambda s: None)
        return api, out

    def test_label_admitted_outsider_is_never_re_measured_on_the_label(self):
        api, out = self.run_stale(as_fork(labels=("boat-ok",)), auto=False)
        self.assertNotIn("dispatches", [w[1] for w in api.writes])
        self.assertTrue(landing_statuses(api)[-1]["description"].startswith("Held: awaiting-readmission."))
        api, _ = self.run_stale(as_fork(labels=("boat-ok",)), auto=True)
        self.assertEqual(api.writes[0][2], {"event_type": "bfme-admit-request", "client_payload": {"number": 2}})

    def test_with_automatic_admission_members_also_go_through_admission(self):
        def member(fx):
            as_fork()(fx)
            fx["pulls/2"]["author_association"] = "MEMBER"
        api, _ = self.run_stale(member, auto=True)
        self.assertEqual(api.writes[0][2], {"event_type": "bfme-admit-request", "client_payload": {"number": 2}})

    def test_member_fork_still_dispatches_directly(self):
        def member(fx):
            as_fork(labels=("boat-ok",))(fx)
            fx["pulls/2"]["author_association"] = "MEMBER"
        api = FakeApi(live=True, main=LIVE_MAIN, edit=member)
        ticks = iter(range(0, 10**7, 900))
        with tempfile.TemporaryDirectory() as tmp:
            quiet(lander.run, ["select", tmp + "/w"], env(main=LIVE_MAIN, mode="live", pr="2"), api=api,
                  config=NO_EXCEPTIONS, clock=lambda: next(ticks), sleep=lambda s: None)
        self.assertEqual(api.writes[0][2]["event_type"], "bfme-remeasure")

    def test_daily_limit_from_admission_is_named(self):
        api = FakeApi(live=False, main=MEASURED, edit=lambda fx: (as_fork()(fx), admission(
            fx, "pending", "Held: daily limit, retried automatically")))
        decision = lander.evaluate(api, 2, MEASURED, NO_EXCEPTIONS)
        self.assertEqual(decision["reasons"][0].code, "daily-limit")
        self.assertIn("you do not need to do anything", decision["reasons"][0].found)


def header_pr(fx, state=None, description=None):
    fx["pulls/2/files?per_page=100&page=1"].append({"filename": "include/GameLogic/Object.h", "status": "modified"})
    if state:
        fx[f"commits/{PR2}/statuses?per_page=100&page=1"].insert(0, {
            "id": 10**12, "context": "BFME II / Header rules", "state": state, "description": description,
            "creator": {"id": lander.lr.BOT_ID}})


AUTO = CONFIG | {"auto_headers": True}


class HeaderRules(unittest.TestCase):
    """With BFME_AUTO_HEADERS=yes a header change lands on the header rules (H1-H8), not on a listed approver."""

    def evaluate(self, state=None, description=None, config=AUTO):
        api = FakeApi(live=False, main=MEASURED, edit=lambda fx: header_pr(fx, state, description))
        return lander.evaluate(api, 2, MEASURED, config)

    def test_passing_head_lands_with_no_approver(self):
        decision = self.evaluate("success", f"Header rules passed (main {MEASURED[:12]})")
        self.assertEqual((decision["stage"], decision["reasons"]), ("land", []))

    def test_switch_off_keeps_the_approver_rule(self):
        decision = self.evaluate("success", f"Header rules passed (main {MEASURED[:12]})", CONFIG)
        self.assertEqual(decision["reasons"][0].code, "header-approvers-empty")

    def test_failure_is_named(self):
        decision = self.evaluate("failure", f"H3: Object defined twice (main {MEASURED[:12]})")
        first = decision["reasons"][0]
        self.assertEqual((first.code, first.final), ("header-rules", True))
        self.assertIn("H3: Object defined twice", first.found)

    def test_ledger_request_waits_and_never_fails(self):
        decision = self.evaluate("pending", "Held: ledger request R-17 (main 879a5be4f21d)")
        first = decision["reasons"][0]
        self.assertEqual((first.code, first.final, decision["ledger_request"]), ("ledger-request", False, True))

    def test_rule_not_built_is_held_by_name(self):
        decision = self.evaluate("pending", "Held: rule not built: H2")
        self.assertEqual(decision["reasons"][0].code, "header-rule-not-built")

    def test_missing_result_is_held(self):
        self.assertEqual(self.evaluate()["reasons"][0].code, "header-rules")

    def test_result_on_an_older_main_is_re_measured(self):
        decision = self.evaluate("success", "Header rules passed (main 000000000000)")
        self.assertEqual((decision["stage"], decision["reasons"]), ("remeasure", []))

    def test_ledger_request_label_is_added_once(self):
        api = FakeApi(live=True, main=MEASURED, edit=lambda fx: header_pr(
            fx, "pending", "Held: ledger request R-17 (main 879a5be4f21d)"))
        with tempfile.TemporaryDirectory() as tmp:
            quiet(lander.run, ["select", tmp + "/w"], env(mode="live", pr="2"), api=api, config=AUTO)
        labels = [w for w in api.writes if w[1] == "issues/2/labels"]
        self.assertEqual(labels, [("POST", "issues/2/labels", {"labels": ["ledger-request"]})])
        api.fx["pulls/2"]["labels"].append({"name": "ledger-request"})
        api.writes.clear()
        with tempfile.TemporaryDirectory() as tmp:
            quiet(lander.run, ["select", tmp + "/w"], env(mode="live", pr="2"), api=api, config=AUTO)
        self.assertEqual([w for w in api.writes if w[1] == "issues/2/labels"], [])


class ProtectedPaths(unittest.TestCase):
    def test_changes_under_protected_paths_are_held_by_the_lander_itself(self):
        for path in ("ci/landing/lander.py", "tools/gates/rules.py", "config/map.json", "expected/a.json",
                     "baseline/game.dat"):
            def edit(fx, path=path):
                fx["pulls/2/files?per_page=100&page=1"].append({"filename": path, "status": "modified"})
            decision = lander.evaluate(FakeApi(live=False, main=MEASURED, edit=edit), 2, MEASURED, CONFIG)
            codes = [item.code for item in decision["reasons"]]
            self.assertIn("protected-path", codes, path)
            self.assertEqual(decision["stage"], "hold")


class ForkLanding(unittest.TestCase):
    def prepare(self, tmp, api):
        code, _ = quiet(lander.run, ["select", tmp + "/w"], env(pr="2", mode="live"), api=api, config=NO_EXCEPTIONS)
        self.assertEqual(code, 0)
        self.assertTrue(Path(tmp, "w/binding.json").is_file())
        put_receipts(tmp + "/w")
        api.writes.clear()

    def merged(self, fx):
        fx["git/ref/heads/main"] = {"object": {"sha": NEW}}
        fx[f"git/commits/{NEW}"] = {"sha": NEW, "parents": [{"sha": MEASURED}], "tree": {"sha": TREE}}

    def test_admitted_fork_lands_like_any_other(self):
        api = FakeApi(live=True, main=MEASURED, edit=as_fork(labels=("boat-ok",)))
        api.responses[("PUT", "pulls/2/merge")] = (200, {"sha": NEW, "merged": True})
        api.after_write[("PUT", "pulls/2/merge")] = self.merged
        with tempfile.TemporaryDirectory() as tmp:
            self.prepare(tmp, api)
            code, out = quiet(lander.run, ["land", tmp + "/w"], env(mode="live"), api=api, config=NO_EXCEPTIONS,
                              sleep=lambda s: None)
        self.assertEqual(code, 0)
        self.assertEqual(api.writes[1][2]["sha"], PR2)
        self.assertIn(f"Landed #2 as {NEW[:12]}", out)

    def test_fork_head_pushed_after_measurement_is_refused_before_merging(self):
        api = FakeApi(live=True, main=MEASURED, edit=as_fork(labels=("boat-ok",)))
        with tempfile.TemporaryDirectory() as tmp:
            self.prepare(tmp, api)
            api.fx["pulls/2"]["head"]["sha"] = "9" * 40
            with self.assertRaises(lander.Held) as caught:
                quiet(lander.run, ["land", tmp + "/w"], env(mode="live"), api=api, config=NO_EXCEPTIONS)
        self.assertEqual(caught.exception.code, "head-changed")
        self.assertNotIn("PUT", [w[0] for w in api.writes])

    def test_fork_head_pushed_at_the_merge_is_refused_by_the_expected_head(self):
        api = FakeApi(live=True, main=MEASURED, edit=as_fork(labels=("boat-ok",)))
        api.responses[("PUT", "pulls/2/merge")] = (409, {"message": "Head branch was modified. Review and try the merge again."})
        with tempfile.TemporaryDirectory() as tmp:
            self.prepare(tmp, api)
            with self.assertRaises(lander.Held) as caught:
                quiet(lander.run, ["land", tmp + "/w"], env(mode="live"), api=api, config=NO_EXCEPTIONS)
        self.assertEqual(caught.exception.code, "head-changed")
        self.assertEqual([w[1] for w in api.writes if w[1].startswith("statuses/") and w[1] != f"statuses/{PR2}"], [])
        self.assertTrue(landing_statuses(api)[-1]["description"].startswith("Held: head-changed."))

    def test_admission_withdrawn_before_merge_holds(self):
        api = FakeApi(live=True, main=MEASURED, edit=as_fork(labels=("boat-ok",)))
        with tempfile.TemporaryDirectory() as tmp:
            self.prepare(tmp, api)
            api.fx["pulls/2"]["labels"] = []
            with self.assertRaises(lander.Held) as caught:
                quiet(lander.run, ["land", tmp + "/w"], env(mode="live"), api=api, config=NO_EXCEPTIONS)
        self.assertEqual(caught.exception.code, "awaiting-admission")
        self.assertNotIn("PUT", [w[0] for w in api.writes])


QUEUE_RULES = [{"type": "merge_queue", "ruleset_source_type": "Repository", "ruleset_id": 1, "parameters": {
    "check_response_timeout_minutes": 30, "grouping_strategy": "ALLGREEN", "max_entries_to_build": 3,
    "max_entries_to_merge": 1, "merge_method": "SQUASH", "min_entries_to_merge": 1,
    "min_entries_to_merge_wait_minutes": 0}}]


def queued(fx):
    fx["rules/branches/main"] = QUEUE_RULES
    fx["pulls/2"]["node_id"] = NODE


class MergeQueue(unittest.TestCase):
    def run_select(self, api, main=MEASURED, mode="live"):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = quiet(lander.run, ["select", tmp + "/w"], env(main=main, mode=mode, pr="2"), api=api,
                              config=CONFIG)
            self.assertFalse(Path(tmp, "w/binding.json").exists())
        self.assertEqual(code, 0)
        return out

    def test_rules_unavailable_means_direct_merging(self):
        self.assertFalse(lander.merge_queue_active(FakeApi(live=False)))
        self.assertTrue(lander.merge_queue_active(FakeApi(live=False, edit=queued)))

    def test_eligible_pull_request_is_queued_with_its_expected_head(self):
        api = FakeApi(live=True, main=MEASURED, edit=queued)
        api.responses[("POST", "graphql")] = (200, {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 1}}}})
        out = self.run_select(api)
        mutation = [w for w in api.writes if w[1] == "graphql"][0][2]
        self.assertEqual(mutation["variables"], {"pr": NODE, "head": PR2})
        self.assertIn("expectedHeadOid: $head", mutation["query"])
        self.assertEqual(landing_statuses(api)[-1]["description"], f"Cleared for the merge queue at head {PR2[:12]}")
        self.assertNotIn("PUT", [w[0] for w in api.writes])
        self.assertNotIn("dispatches", [w[1] for w in api.writes])
        self.assertIn("Merge queue mode", out)

    def test_stale_pull_request_is_queued_without_remeasuring(self):
        api = FakeApi(live=True, main=LIVE_MAIN, edit=queued)
        api.responses[("POST", "graphql")] = (200, {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 1}}}})
        self.run_select(api, main=LIVE_MAIN)
        self.assertIn("graphql", [w[1] for w in api.writes])
        self.assertNotIn("dispatches", [w[1] for w in api.writes])

    def test_already_queued_head_is_not_queued_again(self):
        def again(fx):
            queued(fx)
            fx[f"commits/{PR2}/statuses?per_page=100&page=1"].insert(0, {
                "id": 10**12, "context": lander.LANDING_CONTEXT, "state": "success",
                "description": f"Cleared for the merge queue at head {PR2[:12]}", "creator": {"id": lander.lr.BOT_ID}})
        api = FakeApi(live=True, main=MEASURED, edit=again)
        self.run_select(api)
        self.assertEqual(api.writes, [])

    def test_queue_refusal_is_named_on_the_pull_request(self):
        api = FakeApi(live=True, main=MEASURED, edit=queued)
        api.responses[("POST", "graphql")] = (200, {"errors": [{"message": "Pull request is not mergeable"}]})
        out = self.run_select(api)
        self.assertTrue(landing_statuses(api)[-1]["description"].startswith("Held: enqueue-refused."))
        self.assertIn("refused #2", out)

    def test_held_pull_request_is_not_queued(self):
        api = FakeApi(live=True, main=MEASURED, edit=lambda fx: (queued(fx), as_fork()(fx)))
        self.run_select(api)
        self.assertNotIn("graphql", [w[1] for w in api.writes])

    def test_dry_run_queues_nothing(self):
        api = FakeApi(live=False, main=MEASURED, edit=queued)
        out = self.run_select(api, mode="dry-run")
        self.assertEqual(api.writes, [])
        self.assertIn("would queue", out)


GROUP_SHA = "6" * 40


class QueueCommit(unittest.TestCase):
    """landing.yml's merge_group job: the ruleset also requires BFME II / Landing on each queue commit."""

    def run_group(self, edit=None, ref=f"refs/heads/gh-readonly-queue/main/pr-2-{PR2}", event_name="merge_group"):
        api = FakeApi(live=True, main=MEASURED, edit=edit)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "event.json")
            path.write_text(json.dumps({"merge_group": {"head_sha": GROUP_SHA, "head_ref": ref,
                                                        "base_sha": MEASURED, "base_ref": "refs/heads/main"}}))
            environment = env() | {"GITHUB_EVENT_NAME": event_name, "GITHUB_REF": ref, "GITHUB_EVENT_PATH": str(path),
                                   "BFME_LANDING_MODE": "dry-run"}
            code, out = quiet(lander.run, ["group", tmp + "/w"], environment, api=api, config=CONFIG)
        self.assertEqual(code, 0)
        group = [w[2] for w in api.writes if w[1] == f"statuses/{GROUP_SHA}"]
        self.assertEqual(len(group), 1)
        return group[0], out

    @staticmethod
    def cleared(fx):
        fx[f"commits/{PR2}/statuses?per_page=100&page=1"].insert(0, {
            "id": 10**12, "context": lander.LANDING_CONTEXT, "state": "success",
            "description": f"Cleared for the merge queue at head {PR2[:12]}", "creator": {"id": lander.lr.BOT_ID}})

    def test_queue_commit_of_a_pull_request_the_lander_queued_is_cleared(self):
        written, out = self.run_group(self.cleared)
        self.assertEqual((written["state"], written["context"]), ("success", lander.LANDING_CONTEXT))
        self.assertEqual(written["description"], f"Cleared in the merge queue: #2 head {PR2[:12]}")

    def test_pull_request_queued_another_way_is_refused(self):
        written, _ = self.run_group()
        self.assertEqual(written["state"], "failure")
        self.assertTrue(written["description"].startswith("Held: not-queued-by-landing."))

    def test_clearance_written_by_someone_else_is_refused(self):
        def forged(fx):
            self.cleared(fx)
            fx[f"commits/{PR2}/statuses?per_page=100&page=1"][0]["creator"] = {"id": 1760627}
        written, _ = self.run_group(forged)
        self.assertTrue(written["description"].startswith("Held: not-queued-by-landing."))

    def test_unreadable_queue_ref_is_refused_by_name(self):
        written, _ = self.run_group(self.cleared, ref="refs/heads/gh-readonly-queue/main/something-else")
        self.assertTrue(written["description"].startswith("Held: merge-group-ref."))

    def test_head_moved_after_queueing_is_refused(self):
        def moved(fx):
            self.cleared(fx)
            fx["pulls/2"]["head"]["sha"] = "9" * 40
        written, _ = self.run_group(moved)
        self.assertTrue(written["description"].startswith("Held: head-changed."))

    def test_group_mode_needs_the_merge_group_event(self):
        with self.assertRaises(lander.Failed):
            self.run_group(self.cleared, event_name="workflow_dispatch")


class Clearance(unittest.TestCase):
    def test_leftover_clearance_is_withdrawn(self):
        def cleared(fx):
            fx[f"commits/{PR2}/statuses?per_page=100&page=1"].insert(0, {
                "id": 10**12, "context": lander.LANDING_CONTEXT, "state": "success",
                "description": f"Cleared to land on main {LIVE_MAIN[:12]}", "creator": {"id": lander.lr.BOT_ID}})
        api = FakeApi(live=True, main=LIVE_MAIN, edit=cleared)
        with tempfile.TemporaryDirectory() as tmp:
            quiet(lander.run, ["select", tmp + "/w"], env(main=LIVE_MAIN, mode="live", pr="2"), api=api,
                  config=CONFIG | {"remeasure": "off"})
        self.assertEqual(landing_statuses(api)[0]["state"], "pending")
        self.assertEqual(landing_statuses(api)[0]["description"], "Eligible; waiting for the landing job")

    def test_nothing_to_land_has_a_one_line_result(self):
        api = FakeApi(live=False, main=MEASURED)
        with tempfile.TemporaryDirectory() as tmp:
            _, out = quiet(lander.run, ["select", tmp + "/w"], env(pr="3"), api=api, config=CONFIG)
        self.assertIn("::notice title=BFME II landing::Nothing to land on main 879a5be4f21d: 1 open pull request(s); "
                      "held #3 refused-branch.", out)


if __name__ == "__main__":
    unittest.main()
