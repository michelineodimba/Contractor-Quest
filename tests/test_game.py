import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from contractor import game, missions, runner
from contractor.money import D
from contractor.ui import make_server


def play(mission_id, draft=None, out=None):
    m, d, task, plan, c = runner.make_mission(mission_id, draft or {}, out or tempfile.mkdtemp())
    rep = c.run(task, plan)
    result, receipts, flex = runner.score_mission(m, d, plan, c, rep)
    return m, d, plan, rep, result, receipts, flex


class TestMissions(unittest.TestCase):
    def test_every_mission_runs_and_never_overspends(self):
        for m in missions.MISSIONS:
            _, _, plan, rep, result, _, _ = play(m["id"])
            self.assertLessEqual(rep.spent + rep.held, rep.budget, m["id"])
            self.assertEqual(len(rep.receipts), len(plan.parts))
            self.assertIn(result["grade"], "SABCD")

    def test_optimal_play_earns_s_and_default_does_not(self):
        best = {"launch-brief": {"market_data": {"stars": 3}, "competitor_scan": {"stars": 2}, "risk_review": {"stars": 1}},
                "hard-bargain": {"scope": {"mode": "buy", "stars": 2}, "chain_data": {"stars": 4}, "tokenomics": {"stars": 3}, "security_notes": {"mode": "make"}},
                "scam-alley": {"market_scan": {"mode": "make"}, "risk_notes": {"stars": 5}, "competitor_list": {"stars": 2}},
                "rush-order": {"fx_rates": {"mode": "make"}, "risk_model": {"mode": "make"}, "liquidity_map": {"stars": 2},
                               "compliance_scan": {"stars": 3}}}
        for mid, draft in best.items():
            self.assertEqual(play(mid, draft)[4]["grade"], "S", mid)
            self.assertNotEqual(play(mid)[4]["grade"], "S", mid)

    def test_draft_validation(self):
        m = missions.get_mission("launch-brief")
        d = missions.clean_draft(m, {"market_data": {"mode": "make", "stars": 99}, "outline": {"mode": "buy", "stars": "x"},
                                     "nope": {"mode": "make"}})
        self.assertEqual(d["market_data"]["mode"], "buy")           # not a flex part: mode cannot change
        self.assertEqual(d["market_data"]["stars"], missions.MAX_STARS)   # clamped
        self.assertEqual(d["outline"]["mode"], "buy")               # flex part may change
        self.assertNotIn("nope", d)

    def test_public_view_hides_npc_price_lists(self):
        blob = json.dumps([missions.public_view(m) for m in missions.MISSIONS])
        for key in ('"npcs"', '"price"', '"floor"', '"quality"', '"persona"', "badhash"):
            self.assertNotIn(key, blob)

    def test_unknown_mission(self):
        with self.assertRaises(KeyError):
            runner.make_mission("nope", {}, tempfile.mkdtemp())

    def test_scam_is_caught_not_paid(self):
        _, _, _, rep, result, receipts, _ = play("scam-alley")
        by = {r["part_id"]: r for r in receipts}
        self.assertEqual(by["market_scan"]["status"], "withheld")
        self.assertEqual(by["market_scan"]["total_paid"], "0")
        self.assertIn("scam_caught", game.earned_badges(result, receipts, False))

    def test_all_make_is_legal(self):
        m = missions.get_mission("scam-alley")
        draft = {p["id"]: {"mode": "make"} for p in m["parts"] if p["flex"]}
        _, _, _, rep, result, _, _ = play("scam-alley", draft)
        self.assertEqual(rep.spent, 0)
        self.assertEqual(result["delivered"], result["parts_total"])


class TestScoring(unittest.TestCase):
    def test_same_run_scores_the_same(self):
        a = play("launch-brief")[4]["total"]
        b = play("launch-brief")[4]["total"]
        self.assertEqual(a, b)

    def test_part_points_by_status(self):
        base = {"part_id": "x", "title": "X", "negotiation": [], "notes": [], "total_paid": "5"}
        self.assertEqual(game.part_points({**base, "status": "self_done"}, 0)["total"], 50)
        self.assertEqual(game.part_points({**base, "status": "withheld"}, 5)["total"], 60)
        self.assertEqual(game.part_points({**base, "status": "error"}, 5)["total"], -50)
        paid = {**base, "status": "paid", "negotiation": [{"action": "counter"}, {"action": "accept"}],
                "notes": ["1 revision request(s) sent"]}
        self.assertEqual(game.part_points(paid, 5)["total"], 100 + 40 + 40)

    def test_grade_bounds(self):
        g = {"S": 800, "A": 700, "B": 600}
        self.assertEqual([game.grade_for(x, g) for x in (800, 799, 700, 600, 599, 250, 249)],
                         ["S", "A", "A", "B", "C", "C", "D"])

    def test_rank_for(self):
        self.assertEqual(game.rank_for(0)["name"], "Apprentice")
        self.assertEqual(game.rank_for(800)["name"], "Foreman")
        self.assertIsNone(game.rank_for(99999)["next_at"])


class TestProfile(unittest.TestCase):
    def apply(self, prof, mid="launch-brief", draft=None):
        m, d, plan, rep, result, receipts, flex = play(mid, draft)
        return prof.apply(m, result, receipts, "Ada", flex), result

    def test_first_clear_pays_in_full_and_replay_is_small(self):
        prof = game.Profile(Path(tempfile.mkdtemp()) / "p.json")
        a, res = self.apply(prof)
        self.assertEqual(a["xp_gain"], res["total"])
        b, _ = self.apply(prof)
        self.assertEqual(b["xp_gain"], int(0.1 * res["total"]))
        self.assertFalse(b["personal_best"])

    def test_persists_and_survives_corrupt_file(self):
        path = Path(tempfile.mkdtemp()) / "p.json"
        prof = game.Profile(path)
        self.apply(prof)
        self.assertGreater(game.Profile(path).data["xp"], 0)
        path.write_text("{not json")
        self.assertEqual(game.Profile(path).data["xp"], 0)

    def test_leaderboard_has_ghosts_and_is_sorted(self):
        prof = game.Profile(Path(tempfile.mkdtemp()) / "p.json")
        self.apply(prof)
        rows = prof.leaderboard(missions.get_mission("launch-brief"))
        self.assertTrue(any(r["ghost"] for r in rows))
        self.assertEqual([r["score"] for r in rows], sorted((r["score"] for r in rows), reverse=True))

    def test_reset(self):
        prof = game.Profile(Path(tempfile.mkdtemp()) / "p.json")
        self.apply(prof)
        prof.reset()
        self.assertEqual(prof.data["xp"], 0)
        self.assertEqual(prof.data["best"], {})


class TestWallet(unittest.TestCase):
    def test_account_id_validation(self):
        for ok in ("alice.near", "a-b_c.d.near", "ab", "0123456789abcdef" * 4):
            self.assertTrue(game.valid_account_id(ok), ok)
        for bad in ("", "a", "Alice.near", "a..near", ".near", "a b.near", "x" * 65, "ed25519:abc", "a.near\n", None):
            self.assertFalse(game.valid_account_id(bad), repr(bad))

    def test_link_sets_name_and_unlink(self):
        prof = game.Profile(Path(tempfile.mkdtemp()) / "p.json")
        prof.set_wallet("Ada.near")
        self.assertEqual(prof.data["wallet"], "ada.near")
        m, d, plan, rep, result, receipts, flex = play("launch-brief")
        prof.apply(m, result, receipts, "ignored", flex)
        self.assertEqual(prof.leaderboard(m)[0]["name"], "ada.near") if not prof.leaderboard(m)[0]["ghost"] else None
        self.assertEqual(prof.data["name"], "ada.near")
        with self.assertRaises(ValueError):
            prof.set_wallet("ed25519:SECRETKEY")
        self.assertEqual(prof.data["wallet"], "ada.near")
        prof.set_wallet(None)
        self.assertIsNone(prof.data["wallet"])


class TestGameServer(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()
        self.srv, self.st, self.token = make_server(0, self.out)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, path, body=None, token=True):
        h = {"Content-Type": "application/json"}
        if token:
            h["X-UI-Token"] = self.token
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_missions_endpoint_needs_token_and_hides_prices(self):
        self.assertEqual(self.call("/api/missions", token=False)[0], 403)
        code, body = self.call("/api/missions")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["missions"]), len(missions.MISSIONS))
        self.assertNotIn("badhash", json.dumps(body))

    def test_play_a_mission_end_to_end(self):
        import time
        code, _ = self.call("/api/run", {"mode": "demo", "mission": "launch-brief", "name": "Ada",
                                         "draft": {"market_data": {"stars": 3}, "risk_review": {"stars": 1}}})
        self.assertEqual(code, 202)
        for _ in range(100):
            code, s = self.call("/api/state")
            if s["done"]:
                break
            time.sleep(0.1)
        self.assertIsNone(s["error"])
        self.assertEqual(s["game"]["result"]["grade"], "S")
        self.assertEqual(s["game"]["applied"]["profile"]["name"], "Ada")
        self.assertIn("NEARHackathon", s["game"]["result"]["share"])
        self.assertEqual(len(s["part_points"]), len(s["receipts"]))
        self.assertTrue((Path(self.out) / "profile.json").exists())

    def test_wallet_endpoint(self):
        self.assertEqual(self.call("/api/wallet", {"account_id": "bob.near"}, token=False)[0], 403)
        code, body = self.call("/api/wallet", {"account_id": "bob.near"})
        self.assertEqual(body["profile"]["wallet"], "bob.near")
        self.assertEqual(self.call("/api/wallet", {"account_id": "not valid!"})[0], 400)
        self.assertEqual(self.call("/api/wallet", {"account_id": None})[1]["profile"]["wallet"], None)

    def test_unknown_mission_is_rejected_and_nothing_starts(self):
        code, body = self.call("/api/run", {"mode": "demo", "mission": "nope"})
        self.assertEqual(code, 400)
        self.assertFalse(self.st.running)

    def test_custom_task_is_practice_only(self):
        import time
        task = Path(runner.EXAMPLE).read_text()
        self.call("/api/run", {"mode": "demo", "task": task, "budget": "20"})
        for _ in range(100):
            _, s = self.call("/api/state")
            if s["done"]:
                break
            time.sleep(0.1)
        self.assertIsNone(s["game"])
        self.assertEqual(self.call("/api/missions")[1]["profile"]["xp"], 0)


if __name__ == "__main__":
    unittest.main()
