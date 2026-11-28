"""
Tests for the rules that matter: who can claim what, how money moves, and what
can't be abused.   Run:  python -m unittest discover -s tests -v
"""
import io
import os
import re
import sys
import tempfile
import unittest
from werkzeug.datastructures import MultiDict

# use a throw-away database, never the real one
_tmp = tempfile.mkdtemp()
os.environ["URBANPULSE_DB"] = os.path.join(_tmp, "test.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random  # noqa: E402
from PIL import Image  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import app as urbanpulse  # noqa: E402
import database  # noqa: E402
import intelligence as ai  # noqa: E402

urbanpulse.UPLOAD_DIR = tempfile.mkdtemp()   # test photos never touch the real uploads folder


def noise_photo(seed, brightness=None):
    """A detailed test photo (random noise) — or a flat one if brightness is given."""
    if brightness is not None:
        img = Image.new("RGB", (400, 300), (brightness,) * 3)
    else:
        rnd = random.Random(seed)
        img = Image.new("RGB", (400, 300))
        img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(400 * 300)])
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


class Browser:
    """A logged-in test client that sends the CSRF token like the real pages do."""

    def __init__(self, email=None, password=None):
        self.c = urbanpulse.app.test_client()
        self.token = self._fetch_token("/")
        if email:
            self.c.post("/auth/login", data={"email": email, "password": password, "csrf_token": self.token})
            self.token = self._fetch_token("/dashboard")

    def _fetch_token(self, path):
        html = self.c.get(path, follow_redirects=True).get_data(as_text=True)
        return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)

    def call(self, method, url, json=None, data=None, token=True):
        headers = {"X-CSRF-Token": self.token} if token else {}
        r = self.c.open(url, method=method, json=json, data=data, headers=headers)
        return r.status_code, (r.get_json(silent=True) or {})


def db_one(sql, *params):
    conn = database.get_db()
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def budget(category):
    return db_one("SELECT budget_remaining FROM sponsors WHERE category=?", category)


class IntelligenceTests(unittest.TestCase):
    def test_tf_idf_svm_classifies_drainage_and_severity(self):
        prediction = ai.classify_issue(
            "The drainage water is overflowing near my house",
            "The storm drain is blocked and water is spilling across the sidewalk.",
        )
        self.assertEqual(prediction["category_group"], "Sanitation")
        self.assertEqual(prediction["subcategory"], "Drainage")
        self.assertIn(prediction["severity"], ai.SEVERITY_POINTS)
        self.assertEqual(prediction["model_status"], "real_data")
        self.assertIn("NYC Open Data", prediction["model_dataset"])

    def test_duplicate_detection_requires_nearby_text_match(self):
        existing = [{
            "id": 7, "title": "Drainage water overflowing near my house",
            "description": "The storm drain is blocked and water is spilling across the sidewalk.",
            "category": "Drainage/Sewage", "status": "Reported", "lat": 12.97, "lng": 77.59,
        }]
        matches = ai.find_duplicate_candidates(
            existing[0]["title"], existing[0]["description"], 12.9701, 77.5901, existing
        )
        self.assertEqual(matches[0]["id"], 7)
        self.assertEqual(ai.find_duplicate_candidates(
            existing[0]["title"], existing[0]["description"], 13.5, 77.59, existing
        ), [])

    def test_category_sets_a_floor_and_keywords_escalate(self):
        self.assertEqual(ai.classify_priority("Wire", "hanging low", "Electrical Hazard"), "High")
        self.assertEqual(ai.classify_priority("Bin", "not cleared", "Garbage & Waste"), "Low")
        self.assertEqual(ai.classify_priority("Bin", "next to a school, a child slipped", "Garbage & Waste"), "High")

    def test_keywords_match_whole_words_only(self):
        self.assertEqual(ai.classify_priority("Old fireplace", "decorative fireplace", "Other"), "Low")
        self.assertEqual(ai.classify_priority("Fire", "something is on fire", "Other"), "High")

    def test_waiting_time_raises_priority_and_votes_are_capped(self):
        fresh = ai.impact_score("Medium", 0, database.now_iso(), "Reported")
        old = ai.impact_score("Medium", 0, "2020-01-01T00:00:00", "Reported")
        self.assertGreater(old, fresh)
        self.assertEqual(ai.impact_score("Low", 10, database.now_iso(), "Reported"),
                         ai.impact_score("Low", 500, database.now_iso(), "Reported"))
        self.assertEqual(ai.impact_score("High", 5, database.now_iso(), "Resolved"), -1.0)

    def test_hotspots_use_distance_not_a_fixed_grid(self):
        # three issues ~110 m apart straddling a round-number coordinate (a grid would split them)
        pts = [{"id": i, "lat": 12.9949 + i * 0.0005, "lng": 77.6, "status": "Reported",
                "category": "Pothole", "address": "Main Rd"} for i in range(3)]
        clusters = ai.detect_hotspots(pts)
        self.assertEqual([c["count"] for c in clusters], [3])
        far = pts + [{"id": 9, "lat": 13.5, "lng": 77.6, "status": "Reported", "category": "Pothole", "address": ""}]
        self.assertEqual(len(ai.detect_hotspots(far)), 1)        # the far one is not pulled in
        pts[0]["status"] = "Resolved"
        self.assertEqual(ai.detect_hotspots(pts), [])            # resolved issues don't count

    def test_photo_verification(self):
        def stats(buf):
            path = os.path.join(_tmp, f"{id(buf)}.png")
            with open(path, "wb") as f:
                f.write(buf.getvalue())
            return ai.analyze_photo(path)
        original, good = stats(noise_photo(1)), stats(noise_photo(2))
        self.assertEqual(ai.verify_completion_photo(original, good)[0], "auto_approved")
        self.assertEqual(ai.verify_completion_photo(original, original)[0], "pending_review")      # reused photo
        self.assertEqual(ai.verify_completion_photo(original, stats(noise_photo(0, 5)))[0], "pending_review")  # black
        self.assertEqual(ai.verify_completion_photo(None, None)[0], "pending_review")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        # every test starts from a fresh, freshly-seeded database
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(database.DB_PATH + suffix):
                os.remove(database.DB_PATH + suffix)
        database.init_db(seed_demo_data=True)
        urbanpulse.app.config["TESTING"] = True
        self.admin = Browser("admin@urbanpulse.gov", "Admin@123")
        self.citizen = Browser("citizen@urbanpulse.gov", "Citizen@123")
        self.ravi = Browser("worker@urbanpulse.gov", "Worker@123")       # Electrician, Indiranagar
        self.manoj = Browser("roadcrew@urbanpulse.gov", "Worker@123")    # Road repair, BTM

    def report(self, category="Streetlight", photo=None, who=None):
        data = {"title": "Test issue", "description": "something broke", "category": category,
                "lat": "12.9698", "lng": "77.5910", "address": "Park Road"}
        if photo is not None:
            data["photo"] = (photo, "p.png")
        code, issue = (who or self.citizen).call("POST", "/api/issues", data=data)
        self.assertEqual(code, 201, issue)
        return issue

    def new_electrician(self, name):
        b = Browser()
        b.c.post("/auth/register", data={"name": name, "email": f"{name}@x.in", "password": "password1",
                                         "account_type": "worker", "skill": "Electrician", "csrf_token": b.token})
        b.token = b._fetch_token("/dashboard")
        b.call("POST", "/api/worker/location", json={"lat": 12.9784, "lng": 77.6408, "radius_km": 10})
        return b

    def new_politician(self):
        conn = database.get_db()
        cur = conn.execute(
            """INSERT INTO users (name,email,password_hash,role,portal_role,politician_status,
                                  created_at)
               VALUES (?,?,?,'citizen','politician','approved',?)""",
            ("Test Politician", "politician@test.gov", generate_password_hash("password1"), database.now_iso()),
        )
        conn.commit()
        conn.close()
        browser = Browser()
        browser.c.post("/auth/login", data={
            "email": "politician@test.gov", "password": "password1", "csrf_token": browser.token,
        })
        browser.token = browser._fetch_token("/dashboard")
        return browser, cur.lastrowid

    # --- security basics -------------------------------------------------
    def test_post_without_csrf_token_is_rejected(self):
        code, _ = self.citizen.call("POST", "/api/issues", data={"title": "x"}, token=False)
        self.assertEqual(code, 400)

    def test_roles_are_enforced(self):
        self.assertEqual(self.citizen.call("GET", "/api/sponsors")[0], 403)
        self.assertEqual(self.citizen.call("GET", "/api/worker/jobs")[0], 403)
        self.assertEqual(self.ravi.call("GET", "/api/reviews")[0], 403)

    def test_admin_cannot_be_self_registered_without_the_code(self):
        b = Browser()
        r = b.c.post("/auth/register", data={"name": "Eve", "email": "eve@x.in", "password": "password1",
                                             "account_type": "admin", "admin_code": "guess", "csrf_token": b.token})
        self.assertEqual(r.status_code, 403)

    def test_citizen_can_upvote_and_downvote_own_report(self):
        issue = self.report()
        url = f"/api/issues/{issue['id']}/vote"

        code, body = self.citizen.call("POST", url, json={"direction": "up"})
        self.assertEqual((code, body["upvotes"], body["my_vote"]), (200, 1, 1))

        code, body = self.citizen.call("POST", url, json={"direction": "down"})
        self.assertEqual((code, body["upvotes"], body["downvotes"], body["my_vote"]),
                         (200, 0, 1, -1))

        code, body = self.citizen.call("POST", url, json={"direction": "none"})
        self.assertEqual((code, body["upvotes"], body["downvotes"], body["my_vote"]),
                         (200, 0, 0, 0))

    def test_votes_switch_direction_and_comments_and_follows_are_counted(self):
        issue = self.report()
        url = f"/api/issues/{issue['id']}/vote"
        self.assertEqual(self.ravi.call("POST", url, json={"direction": "up"})[1]["upvotes"], 1)
        changed = self.ravi.call("POST", url, json={"direction": "down"})[1]
        self.assertEqual((changed["upvotes"], changed["downvotes"], changed["my_vote"]), (0, 1, -1))
        comment_code, _ = self.ravi.call(
            "POST", f"/api/issues/{issue['id']}/comments", json={"body": "I saw this too."}
        )
        self.assertEqual(comment_code, 201)
        self.assertTrue(self.ravi.call("POST", f"/api/issues/{issue['id']}/follow")[1]["following"])
        details = self.citizen.call("GET", f"/api/issues/{issue['id']}")[1]
        self.assertEqual(details["comments_count"], 1)
        self.assertEqual(db_one(
            "SELECT civic_xp FROM users WHERE email='worker@urbanpulse.gov'"
        ), 2)

    def test_votes_preserve_existing_issue_upvote_totals(self):
        issue_id = db_one(
            "SELECT id FROM issues WHERE title=?",
            "Open manhole cover near school gate",
        )
        self.assertEqual(db_one("SELECT upvotes FROM issues WHERE id=?", issue_id), 34)
        url = f"/api/issues/{issue_id}/vote"

        code, result = self.ravi.call("POST", url, json={"direction": "up"})
        self.assertEqual((code, result["upvotes"], result["downvotes"]), (200, 35, 0))

        code, result = self.ravi.call("POST", url, json={"direction": "down"})
        self.assertEqual((code, result["upvotes"], result["downvotes"]), (200, 34, 1))

        code, result = self.ravi.call("POST", url, json={"direction": "none"})
        self.assertEqual((code, result["upvotes"], result["downvotes"]), (200, 34, 0))

    def test_issue_accepts_multiple_image_and_video_attachments(self):
        body = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isomiso2"
        data = MultiDict([
            ("title", "Media upload test"), ("description", "A drain is blocked."),
            ("category", "Drainage/Sewage"), ("lat", "12.9698"), ("lng", "77.5910"),
            ("media", (noise_photo(40), "report.png", "image/png")),
            ("media", (io.BytesIO(body), "clip.mp4", "video/mp4")),
        ])
        code, issue = self.citizen.call("POST", "/api/issues", data=data)
        self.assertEqual(code, 201, issue)
        detail = self.citizen.call("GET", f"/api/issues/{issue['id']}")[1]
        self.assertEqual([item["type"] for item in detail["media"]], ["image", "video"])

    def test_new_taxonomy_submission_does_not_require_confirmation_checkbox(self):
        data = {
            "title": "Drainage water overflowing",
            "description": "Water is coming out of the storm drain near the school.",
            "category_group": "Sanitation", "subcategory": "Drainage/Sewage",
            "category": "Drainage/Sewage", "severity": "High",
            "lat": "12.9698", "lng": "77.5910",
        }
        code, result = self.citizen.call("POST", "/api/issues", data=data)
        self.assertEqual(code, 201, result)

    def test_lifecycle_requires_assigned_politician_and_reporter_verification(self):
        politician, politician_id = self.new_politician()
        self.assertTrue(self.citizen.call(
            "POST", f"/api/politicians/{politician_id}/follow"
        )[1]["following"])
        code, issue = self.citizen.call("POST", "/api/issues", data={
            "title": "Blocked drainage overflowing",
            "description": "Water is overflowing from the blocked storm drain near the market.",
            "category": "Drainage/Sewage", "lat": "12.9698", "lng": "77.5910",
            "concerned_politician_id": str(politician_id),
        })
        self.assertEqual(code, 201)
        url = f"/api/issues/{issue['id']}/lifecycle"
        self.assertEqual(politician.call("POST", url, json={"status": "Under Review"})[0], 200)
        self.assertEqual(politician.call("POST", url, json={"status": "Acknowledged"})[0], 200)
        self.assertEqual(politician.call("POST", url, json={"status": "Assigned"})[0], 200)
        self.assertEqual(politician.call("POST", url, json={"status": "In Progress"})[0], 200)
        self.assertEqual(politician.call("POST", url, json={"status": "Resolved"})[0], 200)
        self.assertEqual(politician.call("POST", url, json={"status": "Verified"})[0], 403)
        self.assertEqual(self.citizen.call("POST", url, json={"status": "Verified"})[0], 200)
        profile = politician.call("GET", f"/api/profiles/{politician_id}")[1]
        self.assertEqual((profile["assigned"], profile["resolved"], profile["verified"]), (1, 1, 1))
        self.assertGreaterEqual(profile["civic_xp"], 40)
        self.assertTrue(self.citizen.call("GET", f"/api/profiles/{politician_id}")[1]["followed_by_me"])

    def test_admin_must_choose_approved_politician_at_assignment_stage(self):
        _politician, politician_id = self.new_politician()
        issue = self.report("Streetlight")
        url = f"/api/issues/{issue['id']}/lifecycle"
        self.assertEqual(self.admin.call("POST", url, json={"status": "Under Review"})[0], 200)
        self.assertEqual(self.admin.call("POST", url, json={"status": "Acknowledged"})[0], 200)
        self.assertEqual(self.admin.call("POST", url, json={"status": "Assigned"})[0], 400)
        code, result = self.admin.call(
            "POST", url, json={"status": "Assigned", "concerned_politician_id": politician_id}
        )
        self.assertEqual(code, 200)
        self.assertEqual(result["concerned_politician_id"], politician_id)

    def test_classification_api_returns_duplicate_candidates(self):
        self.report("Drainage/Sewage")
        code, result = self.citizen.call("POST", "/api/classify", json={
            "title": "Test issue",
            "description": "something broke",
            "lat": 12.9698, "lng": 77.591,
        })
        self.assertEqual(code, 200)
        self.assertIn("category_group", result)
        self.assertEqual(result["duplicate_candidates"][0]["title"], "Test issue")

    def test_politician_account_needs_only_admin_approval(self):
        browser = Browser()
        response = browser.c.post("/auth/register", data={
            "name": "Council Member", "email": "member@example.com", "password": "password1",
            "account_type": "politician", "csrf_token": browser.token,
        })
        self.assertEqual(response.status_code, 200)
        pending_login = browser.c.post("/auth/login", data={
            "email": "member@example.com", "password": "password1", "csrf_token": browser.token,
        })
        self.assertEqual(pending_login.status_code, 403)
        politician_id = db_one("SELECT id FROM users WHERE email='member@example.com'")
        self.assertEqual(self.admin.call("POST", f"/api/politicians/{politician_id}/review",
                                         json={"decision": "approve"})[0], 200)
        self.assertEqual(db_one("SELECT politician_status FROM users WHERE id=?", politician_id), "approved")
        approved_login = browser.c.post("/auth/login", data={
            "email": "member@example.com", "password": "password1", "csrf_token": browser.token,
        })
        self.assertEqual(approved_login.status_code, 302)

    def test_existing_and_new_portal_pages_render(self):
        self.assertEqual(self.citizen.c.get("/dashboard").status_code, 200)
        self.assertEqual(self.ravi.c.get("/worker").status_code, 200)
        self.assertEqual(self.admin.c.get("/admin").status_code, 200)
        self.assertEqual(self.citizen.c.get("/politicians").status_code, 200)
        citizen_id = db_one("SELECT id FROM users WHERE email='citizen@urbanpulse.gov'")
        profile = self.citizen.c.get(f"/profile/{citizen_id}")
        self.assertEqual(profile.status_code, 200)
        self.assertIn("Civic XP", profile.get_data(as_text=True))

    # --- claiming ----------------------------------------------------------
    def test_worker_can_only_claim_jobs_matching_their_skill(self):
        issue = self.report("Pothole")
        code, body = self.ravi.call("POST", f"/api/issues/{issue['id']}/claim")
        self.assertEqual(code, 403)
        self.assertIn("skill", body["error"])
        jobs = self.ravi.call("GET", "/api/worker/jobs")[1]["jobs"]
        self.assertTrue(all(j["category"] in ("Electrical Hazard", "Streetlight", "Traffic Signal") for j in jobs))

    def test_claim_escrows_reward_and_release_refunds_it(self):
        issue = self.report()
        before = budget("Streetlight")
        code, body = self.ravi.call("POST", f"/api/issues/{issue['id']}/claim")
        self.assertEqual(code, 200, body)
        self.assertEqual(budget("Streetlight"), before - 120)
        self.assertEqual(body["reward_amount"], 120)
        lifecycle = self.ravi.call(
            "POST", f"/api/issues/{issue['id']}/lifecycle", json={"status": "In Progress"}
        )
        self.assertEqual(lifecycle[1]["public_status"], "In Progress")
        self.assertEqual(self.ravi.call("POST", f"/api/issues/{issue['id']}/release")[0], 200)
        self.assertEqual(budget("Streetlight"), before)
        self.assertEqual(db_one("SELECT status FROM issues WHERE id=?", issue["id"]), "Reported")

    def test_two_workers_cannot_claim_the_same_job(self):
        issue = self.report()
        rival = self.new_electrician("rival1")
        before = budget("Streetlight")
        first = self.ravi.call("POST", f"/api/issues/{issue['id']}/claim")[0]
        second = rival.call("POST", f"/api/issues/{issue['id']}/claim")[0]
        self.assertEqual((first, second), (200, 409))
        self.assertEqual(budget("Streetlight"), before - 120)     # reserved exactly once

    def test_active_claims_are_capped(self):
        worker = self.new_electrician("hoarder")
        codes = [worker.call("POST", f"/api/issues/{self.report()['id']}/claim")[0] for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 409])

    def test_unfunded_category_cannot_be_claimed(self):
        issue = self.report()
        sponsors = self.admin.call("GET", "/api/sponsors")[1]
        for s in sponsors:
            if s["active"] and s["category"] in ("Streetlight", "All"):
                self.admin.call("POST", f"/api/sponsors/{s['id']}/toggle")
        try:
            code, body = self.ravi.call("POST", f"/api/issues/{issue['id']}/claim")
            self.assertEqual(code, 409)
            self.assertIn("sponsor", body["error"])
        finally:
            for s in sponsors:
                if s["active"] and s["category"] in ("Streetlight", "All"):
                    self.admin.call("POST", f"/api/sponsors/{s['id']}/toggle")

    # --- paying ------------------------------------------------------------
    def test_verified_photo_pays_once_and_only_once(self):
        issue = self.report("Streetlight", photo=noise_photo(10))
        self.ravi.call("POST", f"/api/issues/{issue['id']}/claim")
        wallet = db_one("SELECT wallet_balance FROM users WHERE email='worker@urbanpulse.gov'")
        proof = {"note": "Replaced the lamp", "photo": (noise_photo(11), "proof.png")}
        code, body = self.ravi.call("POST", f"/api/issues/{issue['id']}/resolve", data=proof)
        self.assertEqual((code, body["verification_status"], body["reward_paid"]), (200, "auto_approved", 1))
        self.assertEqual(db_one("SELECT wallet_balance FROM users WHERE email='worker@urbanpulse.gov'"), wallet + 120)
        # the old exploits: resolve again, or release the paid job and re-claim it
        again = {"note": "again", "photo": (noise_photo(12), "proof.png")}
        self.assertEqual(self.ravi.call("POST", f"/api/issues/{issue['id']}/resolve", data=again)[0], 403)
        self.assertEqual(self.ravi.call("POST", f"/api/issues/{issue['id']}/release")[0], 403)
        self.assertEqual(db_one("SELECT COUNT(*) FROM reward_ledger WHERE issue_id=?", issue["id"]), 1)

    def test_resolving_requires_a_note_and_a_photo(self):
        issue = self.report()
        self.ravi.call("POST", f"/api/issues/{issue['id']}/claim")
        url = f"/api/issues/{issue['id']}/resolve"
        self.assertEqual(self.ravi.call("POST", url, data={"note": "done"})[0], 400)
        self.assertEqual(self.ravi.call("POST", url, data={"photo": (noise_photo(1), "p.png")})[0], 400)
        self.assertEqual(self.ravi.call("POST", url, data={"note": "x", "photo": (io.BytesIO(b"not an image"), "a.png")})[0], 400)

    def test_flagged_photo_is_held_then_admin_can_approve(self):
        issue = self.report("Streetlight", photo=noise_photo(20))
        self.ravi.call("POST", f"/api/issues/{issue['id']}/claim")
        wallet = db_one("SELECT wallet_balance FROM users WHERE email='worker@urbanpulse.gov'")
        # the worker re-uses the citizen's photo
        code, body = self.ravi.call("POST", f"/api/issues/{issue['id']}/resolve",
                                    data={"note": "done", "photo": (noise_photo(20), "p.png")})
        self.assertEqual((code, body["verification_status"], body["reward_paid"]), (200, "pending_review", 0))
        self.assertEqual(db_one("SELECT wallet_balance FROM users WHERE email='worker@urbanpulse.gov'"), wallet)
        self.assertIn(issue["id"], [r["id"] for r in self.admin.call("GET", "/api/reviews")[1]])
        self.assertEqual(self.ravi.call("POST", f"/api/issues/{issue['id']}/approve-reward")[0], 403)
        self.assertEqual(self.admin.call("POST", f"/api/issues/{issue['id']}/approve-reward")[0], 200)
        self.assertEqual(db_one("SELECT wallet_balance FROM users WHERE email='worker@urbanpulse.gov'"), wallet + 120)
        self.assertEqual(self.admin.call("POST", f"/api/issues/{issue['id']}/approve-reward")[0], 409)   # not twice

    def test_rejection_sends_job_back_to_same_worker_with_escrow_intact(self):
        issue = self.report("Streetlight", photo=noise_photo(30))
        self.ravi.call("POST", f"/api/issues/{issue['id']}/claim")
        self.ravi.call("POST", f"/api/issues/{issue['id']}/resolve",
                       data={"note": "done", "photo": (noise_photo(0, 3), "dark.png")})
        locked = budget("Streetlight")
        code, body = self.admin.call("POST", f"/api/issues/{issue['id']}/reject-reward", json={"note": "too dark"})
        self.assertEqual((code, body["status"], body["claimed_by_me"]), (200, "Claimed", False))
        self.assertEqual(budget("Streetlight"), locked)           # still reserved for the same worker
        mine = [j["id"] for j in self.ravi.call("GET", "/api/worker/my-jobs")[1]]
        self.assertIn(issue["id"], mine)
        code, body = self.ravi.call("POST", f"/api/issues/{issue['id']}/resolve",
                                    data={"note": "retaken", "photo": (noise_photo(31), "better.png")})
        self.assertEqual(body["verification_status"], "auto_approved")

    # --- admin overrides ---------------------------------------------------
    def test_admin_cannot_resolve_a_worker_held_job_but_can_take_it_back(self):
        issue = self.report()
        before = budget("Streetlight")
        self.ravi.call("POST", f"/api/issues/{issue['id']}/claim")
        url = f"/api/issues/{issue['id']}/status"
        self.assertEqual(self.admin.call("POST", url, json={"status": "Resolved"})[0], 409)
        self.assertEqual(self.admin.call("POST", url, json={"status": "Claimed"})[0], 400)
        code, body = self.admin.call("POST", url, json={"status": "Reported"})
        self.assertEqual((code, body["status"], body["claimed_by"]), (200, "Reported", None))
        self.assertEqual(budget("Streetlight"), before)           # escrow refunded

    def test_resolved_issues_are_final(self):
        resolved = db_one("SELECT id FROM issues WHERE status='Resolved' LIMIT 1")
        self.assertEqual(self.admin.call("POST", f"/api/issues/{resolved}/status", json={"status": "Reported"})[0], 409)

    # --- misc --------------------------------------------------------------
    def test_csv_export_defuses_spreadsheet_formulas(self):
        self.report().get("id")
        self.citizen.call("POST", "/api/issues", data={"title": "=HYPERLINK(\"http://evil\")", "description": "x",
                                                       "category": "Other", "lat": "12.9", "lng": "77.6"})
        r = self.admin.c.get("/api/export/csv")
        self.assertIn("'=HYPERLINK", r.get_data(as_text=True))

    def test_hotspot_endpoint_finds_the_seeded_btm_cluster(self):
        clusters = self.admin.call("GET", "/api/hotspots")[1]
        self.assertTrue(any("BTM" in c["area"] and c["count"] >= 3 for c in clusters))


if __name__ == "__main__":
    unittest.main()
