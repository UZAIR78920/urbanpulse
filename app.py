"""
UrbanPulse — a citizen-driven framework for real-time civic issue intelligence.
app.py — Flask application (routes, auth, REST API, live stream).

Run:  python app.py      then open  http://127.0.0.1:5000

Three roles
  citizen  reports issues, upvotes, follows progress
  worker   skilled professional: claims ("locks") a nearby job that matches their
           skill, fixes it, uploads proof, and is paid a sponsor-funded reward
  admin    municipal office: oversight, sponsor budgets, review of flagged payouts

Reward lifecycle (the part worth understanding first)
  claim    sponsor budget is ESCROWED on the issue  -> the promised reward is guaranteed
  resolve  photo auto-verified -> escrow PAID to the worker's wallet
           photo flagged       -> held until an admin approves (pay) or rejects (redo)
  release  escrow is REFUNDED to the sponsor and the job re-opens
"""

import csv
import hmac
import io
import json
import logging
import math
import os
import queue
import re
import secrets
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import wraps

from flask import (Flask, Response, abort, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from PIL import Image, ImageOps
from werkzeug.security import check_password_hash, generate_password_hash

import database
from database import now_iso
from intelligence import (analyze_photo, classify_issue, classify_priority,
                          detect_hotspots, find_duplicate_candidates,
                          haversine_km, impact_score, verify_completion_photo)

app = Flask(__name__)
# A random key per start is safer than a hard-coded one in source; set
# URBANPULSE_SECRET to keep sessions alive across restarts / multiple processes.
app.config["SECRET_KEY"] = os.environ.get("URBANPULSE_SECRET") or secrets.token_hex(32)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads", "issues")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Admin accounts can't be self-created by anyone on the public form: that would let a
# stranger approve payouts and edit sponsor budgets. Registration needs this code.
ADMIN_INVITE_CODE = os.environ.get("URBANPULSE_ADMIN_CODE", "URBANPULSE-ADMIN-2026")

# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------
CATEGORIES = [
    "Pothole", "Garbage & Waste", "Streetlight", "Water Supply", "Drainage/Sewage",
    "Road Damage", "Electrical Hazard", "Illegal Construction", "Traffic Signal", "Other",
]
REPORT_CATEGORIES = [
    "Roads and Infrastructure", "Water Supply", "Electricity",
    "Sanitation", "Public Safety", "Transportation",
]
SUBCATEGORY_GROUPS = {
    "Roads and Infrastructure": ["Pothole", "Road Damage", "Illegal Construction"],
    "Water Supply": ["Water Supply", "Water Leakage", "Contaminated Water"],
    "Electricity": ["Electrical Hazard", "Streetlight", "Power Outage"],
    "Sanitation": ["Drainage", "Garbage & Waste", "Public Toilets"],
    "Public Safety": ["Open Manhole", "Fire Hazard", "Pedestrian Safety", "Environmental Hazard"],
    "Transportation": ["Traffic Signal", "Bus Stop", "Pedestrian Crossing"],
}
SUBCATEGORY_GROUP = {
    subcategory: category
    for category, subcategories in SUBCATEGORY_GROUPS.items()
    for subcategory in subcategories
}
SUBCATEGORY_CANONICAL = {"Drainage": "Drainage/Sewage"}
LEGACY_CATEGORY_GROUP = {
    "Pothole": "Roads and Infrastructure", "Road Damage": "Roads and Infrastructure",
    "Illegal Construction": "Roads and Infrastructure", "Water Supply": "Water Supply",
    "Electrical Hazard": "Electricity", "Streetlight": "Electricity",
    "Drainage/Sewage": "Sanitation", "Garbage & Waste": "Sanitation",
    "Traffic Signal": "Transportation", "Other": "Public Safety",
}
STATUSES = ["Reported", "Claimed", "In Progress", "Resolved"]
# Statuses an admin may set by hand. "Claimed" is excluded because it only means
# something when a worker really holds the lock (and the reward escrow).
ADMIN_STATUSES = ["Reported", "In Progress", "Resolved"]
LIFECYCLE_STATUSES = [
    "Reported", "Under Review", "Acknowledged", "Assigned",
    "In Progress", "Resolved", "Verified",
]
SEVERITIES = ["Low", "Medium", "High"]

# Which problems each profession can legitimately fix. "Illegal Construction" is
# an enforcement matter for the municipality, not a job for a field worker, so
# nobody can claim it.
SKILL_CATEGORY_MAP = {
    "Electrician": ["Electrical Hazard", "Streetlight", "Traffic Signal"],
    "Plumber": ["Water Supply", "Drainage/Sewage"],
    "Road Repair Technician": ["Pothole", "Road Damage"],
    "Sanitation Worker": ["Garbage & Waste", "Drainage/Sewage"],
    "General Handyman": [c for c in CATEGORIES if c != "Illegal Construction"],
}
SKILLS = list(SKILL_CATEGORY_MAP)

DEFAULT_RADIUS_KM = 10
# A claim is a lock on a public job; without a cap one worker could lock the whole board.
MAX_ACTIVE_CLAIMS = 3
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}
MAX_MEDIA_FILES = 5
MAX_VIDEO_BYTES = 20 * 1024 * 1024
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# Live updates (Server-Sent Events): one in-memory queue per connected browser
# ---------------------------------------------------------------------------
_subscribers, _subscribers_lock = [], threading.Lock()


def broadcast(event_type, issue):
    """Push a small public summary of an issue. Clients re-fetch anything they need,
    so no per-user data (votes, claims, rewards) is ever sent to everybody."""
    keys = ("id", "title", "category", "status", "lifecycle_status", "priority",
            "lat", "lng", "reporter_id", "updated_at")
    data = json.dumps({"type": event_type, "payload": {k: issue[k] for k in keys}})
    with _subscribers_lock:
        for q in list(_subscribers):
            try:
                q.put_nowait(data)
            except queue.Full:          # a stalled browser must not block everyone else
                _subscribers.remove(q)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = database.get_db()
    return g.db


@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# One query returns an issue together with the names / vote flag the UI needs, instead
# of 3-4 extra queries per issue.
ISSUE_SQL = """
SELECT i.*,
       cu.name AS claimed_by_name,  cu.skill AS claimed_by_skill,
       ru.name AS resolved_by_name, ru.skill AS resolved_by_skill,
       pu.name AS politician_name,
       EXISTS(SELECT 1 FROM votes v WHERE v.issue_id = i.id AND v.user_id = ? AND v.direction = 1) AS voted_by_me,
       COALESCE((SELECT v.direction FROM votes v WHERE v.issue_id = i.id AND v.user_id = ?), 0) AS my_vote,
       COALESCE((SELECT COUNT(*) FROM votes v WHERE v.issue_id = i.id AND v.direction = -1), 0) AS downvotes,
       EXISTS(SELECT 1 FROM issue_follows f WHERE f.issue_id = i.id AND f.user_id = ?) AS followed_by_me,
       (SELECT COUNT(*) FROM issue_comments c WHERE c.issue_id = i.id) AS comments_count,
       (SELECT COUNT(*) FROM issue_follows f WHERE f.issue_id = i.id) AS followers_count
FROM issues i
LEFT JOIN users cu ON cu.id = i.claimed_by
LEFT JOIN users ru ON ru.id = i.resolved_by
LEFT JOIN users pu ON pu.id = i.concerned_politician_id
"""


def fetch_issues(db, where="", params=()):
    uid = session.get("user_id")
    return db.execute(ISSUE_SQL + where, (uid, uid, uid, *params)).fetchall()


def photo_url(name):
    return url_for("static", filename=f"uploads/issues/{name}") if name else None


def serialize_issue(row, quotes=None):
    d = dict(row)
    d["voted_by_me"] = bool(d["voted_by_me"])
    d["my_vote"] = int(d["my_vote"])
    d["claimed_by_me"] = d["claimed_by"] is not None and d["claimed_by"] == session.get("user_id")
    d["is_locked"] = d["status"] == "Claimed"
    d["impact_score"] = impact_score(d["priority"], d["upvotes"], d["created_at"], d["status"])
    created = datetime.fromisoformat(d["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_days = max(0, (datetime.now(timezone.utc) - created).total_seconds() / 86400)
    d["popularity_score"] = round(max(
        0, d["upvotes"] * 2 - d["downvotes"] + d["comments_count"] * 2
        + d["followers_count"] + {"High": 12, "Medium": 6, "Low": 0}.get(d["severity"], 0)
        + max(0, 20 - age_days)
    ), 1)
    d["public_status"] = d["lifecycle_status"] or d["status"]
    d["photo_url"] = photo_url(d["photo_path"])
    d["completion_photo_url"] = photo_url(d["completion_photo_path"])
    if d["status"] == "Reported" and quotes is not None:
        amount, sponsor = quotes.get(d["category"], (0, None))
        d["estimated_reward"], d["estimated_sponsor"] = amount, sponsor
    return d


def award_xp(db, user_id, issue_id, event_type, points):
    cur = db.execute(
        """INSERT OR IGNORE INTO xp_events (user_id, issue_id, event_type, points, created_at)
           VALUES (?,?,?,?,?)""",
        (user_id, issue_id, event_type, points, now_iso()),
    )
    if cur.rowcount:
        db.execute("UPDATE users SET civic_xp=civic_xp+? WHERE id=?", (points, user_id))





def log_status(db, issue_id, status, note, when):
    db.execute("INSERT INTO status_log (issue_id, status, note, changed_at) VALUES (?,?,?,?)",
               (issue_id, status, note, when))


def get_issue_or_404(db, issue_id):
    row = db.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    return row


def reload_and_broadcast(db, issue_id, event="issue_updated"):
    issue = serialize_issue(fetch_issues(db, " WHERE i.id = ?", (issue_id,))[0], reward_quotes(db))
    broadcast(event, issue)
    return issue


# ---------------------------------------------------------------------------
# Sponsors & the reward escrow
# ---------------------------------------------------------------------------
def best_sponsor(sponsors, category):
    """The sponsor that would fund this category right now: an exact-category sponsor
    beats an 'All' sponsor, then the higher reward wins. Pure function over a list."""
    usable = [s for s in sponsors
              if s["active"] and s["budget_remaining"] >= s["reward_per_issue"]
              and s["category"] in (category, "All")]
    return max(usable, key=lambda s: (s["category"] == category, s["reward_per_issue"]), default=None)


def reward_quotes(db):
    """{category: (reward, sponsor name)} for every category — computed once per request."""
    sponsors = db.execute("SELECT * FROM sponsors WHERE active = 1").fetchall()
    quotes = {}
    for c in CATEGORIES:
        s = best_sponsor(sponsors, c)
        quotes[c] = (s["reward_per_issue"], s["name"]) if s else (0, None)
    return quotes


def pay_reward(db, issue_id, worker_id, now):
    """Move an issue's escrow into the worker's wallet and write the ledger row.
    `reward_paid = 0` in the WHERE clause makes a second payout for the same job impossible."""
    cur = db.execute("UPDATE issues SET reward_paid = 1 WHERE id = ? AND reward_paid = 0 "
                     "AND reward_sponsor_id IS NOT NULL", (issue_id,))
    if cur.rowcount == 0:
        raise ValueError("no unpaid reward is reserved for this job")
    issue = db.execute("SELECT reward_amount, reward_sponsor_id FROM issues WHERE id=?", (issue_id,)).fetchone()
    db.execute("UPDATE users SET wallet_balance = wallet_balance + ? WHERE id = ?",
               (issue["reward_amount"], worker_id))
    db.execute("INSERT INTO reward_ledger (issue_id, worker_id, sponsor_id, amount, created_at) VALUES (?,?,?,?,?)",
               (issue_id, worker_id, issue["reward_sponsor_id"], issue["reward_amount"], now))
    return issue["reward_amount"]


def release_claim(db, issue, note, now):
    """Re-open a claimed job and refund its escrow to the sponsor."""
    if issue["reward_sponsor_id"] and not issue["reward_paid"]:
        db.execute("UPDATE sponsors SET budget_remaining = budget_remaining + ? WHERE id = ?",
                   (issue["reward_amount"], issue["reward_sponsor_id"]))
    delete_photo(issue["completion_photo_path"])
    db.execute("""UPDATE issues SET status='Reported', claimed_by=NULL, claimed_at=NULL,
                  reward_amount=0, reward_sponsor_id=NULL, resolved_by=NULL, resolution_note=NULL,
                  completion_photo_path=NULL, verification_status=NULL, verification_notes=NULL,
                  verification_score=NULL, lifecycle_status='Reported', updated_at=? WHERE id=?""",
               (now, issue["id"]))
    log_status(db, issue["id"], "Reported", note, now)


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------
def save_photo(file_storage):
    """Decode, validate, orient, strip metadata and store as a fresh JPEG under a random
    name. The file's content is what's trusted — never its name or claimed MIME type."""
    try:
        img = Image.open(file_storage.stream)
        if img.format not in ALLOWED_IMAGE_FORMATS:
            raise ValueError
        img = ImageOps.exif_transpose(img).convert("RGB")   # rotate upright BEFORE the metadata is dropped
        img.thumbnail((1600, 1600))
        name = f"{uuid.uuid4().hex}.jpg"
        img.save(os.path.join(UPLOAD_DIR, name), "JPEG", quality=85, optimize=True)
        return name
    except Exception:
        raise ValueError("That file isn't a valid PNG, JPG, WEBP or GIF image.")


def save_video(file_storage):
    head = file_storage.stream.read(16)
    file_storage.stream.seek(0)
    if len(head) < 8:
        raise ValueError("That video file is empty or invalid.")
    if head[4:8] == b"ftyp":
        extension = ".mp4"
    elif head[:4] == b"\x1a\x45\xdf\xa3":
        extension = ".webm"
    else:
        raise ValueError("Videos must be valid MP4 or WebM files.")
    content = file_storage.stream.read(MAX_VIDEO_BYTES + 1)
    if len(content) > MAX_VIDEO_BYTES:
        raise ValueError("Each video must be 20 MB or smaller.")
    name = f"{uuid.uuid4().hex}{extension}"
    with open(os.path.join(UPLOAD_DIR, name), "wb") as target:
        target.write(content)
    return name


def save_issue_media(files):
    if len(files) > MAX_MEDIA_FILES:
        raise ValueError(f"Attach no more than {MAX_MEDIA_FILES} files to a report.")
    saved = []
    try:
        for item in files:
            if not item or not item.filename:
                continue
            if item.mimetype.startswith("image/"):
                saved.append(("image", save_photo(item)))
            elif item.mimetype.startswith("video/"):
                saved.append(("video", save_video(item)))
            else:
                raise ValueError("Attachments must be images or MP4/WebM videos.")
    except ValueError:
        for _kind, name in saved:
            delete_photo(name)
        raise
    return saved


def delete_photo(name):
    if name:
        try:
            os.remove(os.path.join(UPLOAD_DIR, name))
        except OSError:
            pass


def photo_stats(name):
    return analyze_photo(os.path.join(UPLOAD_DIR, name)) if name else None


# ---------------------------------------------------------------------------
# Security: CSRF + role guards
# ---------------------------------------------------------------------------
@app.before_request
def csrf_protect():
    """Every state-changing request must echo the per-session token, so a foreign
    site can't make a logged-in user's browser submit actions on their behalf."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        expected = session.get("csrf_token", "")
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        if not expected or not hmac.compare_digest(sent.encode(), expected.encode()):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Your session expired — please reload the page."}), 400
            abort(400)


def api_error(message, code=400):
    return jsonify({"error": message}), code


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/api/") or request.path == "/stream":
                return api_error("Authentication required", 401)
            return redirect(url_for("landing"))
        return view(*args, **kwargs)
    return wrapped


def role_required(role):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if session.get("role") != role:
                if request.path.startswith("/api/"):
                    return api_error(f"{role.capitalize()} access required", 403)
                return redirect(url_for("console"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


admin_required, worker_required = role_required("admin"), role_required("worker")


def home_for_role(role):
    return url_for({"admin": "admin_page", "worker": "worker_page"}.get(role, "console"))


@app.context_processor
def inject_globals():
    session.setdefault("csrf_token", secrets.token_hex(16))
    return {
        "csrf_token": session["csrf_token"],
        "current_user": {"id": session.get("user_id"), "name": session.get("name"), "role": session.get("role")},
        "categories": CATEGORIES, "statuses": STATUSES, "admin_statuses": ADMIN_STATUSES, "skills": SKILLS,
        "report_categories": REPORT_CATEGORIES, "subcategory_groups": SUBCATEGORY_GROUPS,
        "lifecycle_statuses": LIFECYCLE_STATUSES, "severities": SEVERITIES,
    }


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def landing_page(error=None, mode="login", account_type="citizen", status=200):
    db = get_db()
    stats = {
        "reported": db.execute("SELECT COUNT(*) FROM issues").fetchone()[0],
        "resolved": db.execute("SELECT COUNT(*) FROM issues WHERE status='Resolved'").fetchone()[0],
        "paid": db.execute("SELECT COALESCE(SUM(amount),0) FROM reward_ledger").fetchone()[0],
    }
    return render_template("landing.html", error=error, mode=mode, account_type=account_type, stats=stats), status


@app.route("/")
def landing():
    if session.get("user_id"):
        return redirect(home_for_role(session.get("role")))
    return landing_page()


@app.route("/dashboard")
@login_required
def console():
    return render_template("console.html")


@app.route("/worker")
@login_required
@worker_required
def worker_page():
    skill = get_db().execute("SELECT skill FROM users WHERE id=?", (session["user_id"],)).fetchone()["skill"]
    return render_template("worker.html", skill=skill, my_categories=SKILL_CATEGORY_MAP[skill],
                           max_claims=MAX_ACTIVE_CLAIMS)


@app.route("/admin")
@login_required
@admin_required
def admin_page():
    return render_template("admin.html")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def start_session(user):
    session.clear()          # fresh session on every login: no fixation, no leftover state
    role = "politician" if user["portal_role"] == "politician" else user["role"]
    session.update(user_id=user["id"], name=user["name"], role=role)


@app.route("/auth/register", methods=["POST"])
def register():
    db = get_db()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    role = request.form.get("account_type", "citizen")
    skill = request.form.get("skill", "").strip()
    admin_code = request.form.get("admin_code", "")

    def fail(message, code=400):
        return landing_page(
            message, "register",
            role if role in ("citizen", "worker", "admin", "politician") else "citizen", code
        )

    if role not in ("citizen", "worker", "admin", "politician"):
        role = "citizen"
    if not name or len(name) > 100:
        return fail("Please enter your name.")
    if not EMAIL_RE.match(email):
        return fail("Please enter a valid email address.")
    if len(password) < 8:
        return fail("Password must be at least 8 characters.")
    if role == "worker" and skill not in SKILLS:
        return fail("Please choose your profession.")
    if role == "admin" and not hmac.compare_digest(admin_code.encode(), ADMIN_INVITE_CODE.encode()):
        app.logger.warning("Rejected admin registration for %s from %s (bad invite code)", email, request.remote_addr)
        return fail("That admin invite code isn't valid.", 403)
    if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        return fail("An account with this email already exists.")

    if role == "politician":
        cur = db.execute(
            """INSERT INTO users (name,email,password_hash,role,portal_role,politician_title,
                                  politician_constituency,politician_status,created_at)
               VALUES (?,?,?,'citizen','politician',?,?,'pending',?)""",
            (name, email, generate_password_hash(password),
             request.form.get("politician_title", "").strip()[:100],
             request.form.get("politician_constituency", "").strip()[:120],
             now_iso()),
        )
        db.commit()
        return landing_page(
            "Your politician account was submitted for administrator approval.",
            "login", "politician",
        )

    cur = db.execute(
        """INSERT INTO users (name,email,password_hash,role,portal_role,skill,created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (name, email, generate_password_hash(password), role, role,
         skill if role == "worker" else None, now_iso()),
    )
    db.commit()
    start_session({"id": cur.lastrowid, "name": name, "role": role, "portal_role": role})
    return redirect(home_for_role(role))


@app.route("/auth/login", methods=["POST"])
def login():
    email = request.form.get("email", "").strip().lower()
    user = get_db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not check_password_hash(user["password_hash"], request.form.get("password", "")):
        return landing_page("Incorrect email or password.", status=401)
    if user["portal_role"] == "politician":
        if user["politician_status"] != "approved":
            return landing_page("Your politician account is awaiting administrator approval.", status=403)
    start_session(user)       # the account's stored role decides where they land — no role picker needed
    role = "politician" if user["portal_role"] == "politician" else user["role"]
    return redirect(home_for_role(role))


@app.route("/auth/logout")
def logout():
    session.clear()
    return redirect(url_for("landing"))


# ---------------------------------------------------------------------------
# API — issues (public map)
# ---------------------------------------------------------------------------
@app.route("/api/issues")
@login_required
def api_list_issues():
    db = get_db()
    where, params = [], []
    if request.args.get("category"):
        where.append("i.category = ?")
        params.append(request.args["category"])
    if request.args.get("category_group"):
        where.append("i.category_group = ?")
        params.append(request.args["category_group"])
    if request.args.get("status"):
        status = request.args["status"]
        if status in LIFECYCLE_STATUSES:
            where.append("i.lifecycle_status = ?")
        else:
            where.append("i.status = ?")
        params.append(status)
    q = request.args.get("q", "").strip()
    if q:
        where.append("(i.title LIKE ? OR i.description LIKE ? OR i.address LIKE ?)")
        params += [f"%{q}%"] * 3
    if request.args.get("mine") == "1":
        where.append("i.reporter_id = ?")
        params.append(session["user_id"])

    rows = fetch_issues(db, (" WHERE " + " AND ".join(where)) if where else "", params)
    quotes = reward_quotes(db)
    issues = [serialize_issue(r, quotes) for r in rows]
    if request.args.get("sort") == "urgent":
        issues.sort(key=lambda x: x["impact_score"], reverse=True)
    else:
        issues.sort(key=lambda x: (x["popularity_score"], x["impact_score"]), reverse=True)
    return jsonify(issues)


@app.route("/api/classify", methods=["POST"])
@login_required
def api_classify_issue():
    data = request.get_json(silent=True) or {}
    title = str(data.get("title", "")).strip()[:200]
    description = str(data.get("description", "")).strip()[:4000]
    if len(title) + len(description) < 8:
        return api_error("Add a little more detail to get a useful suggestion.")
    prediction = classify_issue(title, description)
    try:
        lat, lng = float(data["lat"]), float(data["lng"])
    except (KeyError, TypeError, ValueError):
        prediction["duplicate_candidates"] = []
        return jsonify(prediction)
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return api_error("That location looks invalid.")
    rows = get_db().execute(
        """SELECT id,title,description,category,status,lat,lng FROM issues
           WHERE lifecycle_status!='Verified' ORDER BY created_at DESC LIMIT 500"""
    ).fetchall()
    prediction["duplicate_candidates"] = find_duplicate_candidates(
        title, description, lat, lng, [dict(row) for row in rows]
    )
    return jsonify(prediction)


@app.route("/api/issues/<int:issue_id>")
@login_required
def api_get_issue(issue_id):
    db = get_db()
    rows = fetch_issues(db, " WHERE i.id = ?", (issue_id,))
    if not rows:
        return api_error("Issue not found", 404)
    issue = serialize_issue(rows[0], reward_quotes(db))
    issue["timeline"] = [dict(r) for r in db.execute(
        "SELECT status, note, changed_at FROM status_log WHERE issue_id=? ORDER BY id", (issue_id,))]
    reporter = db.execute("SELECT name FROM users WHERE id=?", (issue["reporter_id"],)).fetchone()
    issue["reporter_name"] = reporter["name"] if reporter else "Unknown"
    issue["media"] = [
        {"type": row["media_type"], "url": photo_url(row["file_path"])}
        for row in db.execute("SELECT media_type,file_path FROM issue_media WHERE issue_id=? ORDER BY id", (issue_id,))
    ]
    return jsonify(issue)


@app.route("/api/issues", methods=["POST"])
@login_required
def api_create_issue():
    if session.get("role") == "politician":
        return api_error("Politician accounts cannot submit citizen reports.", 403)
    db = get_db()
    data = request.form
    title = data.get("title", "").strip()
    description = data.get("description", "").strip()
    category = data.get("category") or data.get("subcategory") or "Other"
    category_group = data.get("category_group", "")
    subcategory = data.get("subcategory", "")
    address = data.get("address", "").strip()[:200]

    try:
        lat, lng = float(data.get("lat")), float(data.get("lng"))
    except (TypeError, ValueError):
        return api_error("Please set the issue's location — use your current location or tap the map.")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return api_error("That location looks invalid.")
    if not title or not description:
        return api_error("Title and description are required.")
    if len(title) > 200 or len(description) > 4000:
        return api_error("Title (max 200) or description (max 4000 characters) is too long.")
    if category in REPORT_CATEGORIES:
        category_group = category
        category = subcategory or ""
    elif category in SUBCATEGORY_GROUP:
        category_group = category_group or SUBCATEGORY_GROUP[category]
        subcategory = category
    elif category in CATEGORIES:
        category_group = category_group or LEGACY_CATEGORY_GROUP.get(category, "Public Safety")
        subcategory = category
    else:
        category, subcategory = "Other", "Other"
        category_group = category_group if category_group in REPORT_CATEGORIES else "Public Safety"
    category = SUBCATEGORY_CANONICAL.get(category, category)
    if category_group not in REPORT_CATEGORIES:
        return api_error("Choose a valid issue category.")
    if subcategory not in SUBCATEGORY_GROUPS[category_group] and subcategory not in CATEGORIES:
        return api_error("Choose a valid issue subcategory for the selected category.")
    try:
        severity = data.get("severity", "").strip()
        if severity and severity not in SEVERITIES:
            return api_error("Severity must be Low, Medium or High.")
        severity = severity or classify_issue(title, description)["severity"]
        severity = max(severity, classify_priority(title, description, category), key={
            "Low": 0, "Medium": 1, "High": 2
        }.get)
        reported_date = date.fromisoformat(data.get("reported_date", "") or date.today().isoformat()).isoformat()
    except ValueError:
        return api_error("Reporting date must be a valid YYYY-MM-DD date.")
    politician_id = data.get("concerned_politician_id", "").strip()
    if politician_id:
        try:
            politician_id = int(politician_id)
        except (TypeError, ValueError):
            return api_error("Choose a valid concerned politician.")
        politician = db.execute(
            """SELECT 1 FROM users WHERE id=? AND portal_role='politician'
               AND politician_status='approved'""",
            (politician_id,),
        ).fetchone()
        if not politician:
            return api_error("The selected politician is not an active verified account.")
    else:
        politician_id = None

    uploads = request.files.getlist("media")
    legacy_photo = request.files.get("photo")
    if legacy_photo and legacy_photo.filename:
        uploads.insert(0, legacy_photo)
    try:
        media_files = save_issue_media(uploads)
    except ValueError as e:
        return api_error(str(e))
    photo_name = next((name for kind, name in media_files if kind == "image"), None)

    now = now_iso()
    cur = db.execute(
        """INSERT INTO issues (title,description,category,lat,lng,address,photo_path,status,priority,
                               reporter_id,created_at,updated_at,category_group,subcategory,severity,
                               concerned_politician_id,lifecycle_status,reported_date)
           VALUES (?,?,?,?,?,?,?,'Reported',?,?,?,?,?,?,?,?, 'Reported',?)""",
        (title, description, category, lat, lng, address, photo_name, severity,
         session["user_id"], now, now, category_group, subcategory, severity, politician_id, reported_date),
    )
    for media_type, file_path in media_files:
        db.execute(
            "INSERT INTO issue_media (issue_id,media_type,file_path,created_at) VALUES (?,?,?,?)",
            (cur.lastrowid, media_type, file_path, now),
        )
    log_status(db, cur.lastrowid, "Reported", "Issue reported by citizen.", now)
    db.commit()
    issue = reload_and_broadcast(db, cur.lastrowid, "new_issue")
    issue["ml_suggestion"] = classify_issue(title, description)
    return jsonify(issue), 201


@app.errorhandler(413)
def too_large(_e):
    return api_error("That upload is too large (max 25 MB per request).", 413)


@app.route("/api/issues/<int:issue_id>/upvote", methods=["POST"])
@login_required
def api_upvote(issue_id):
    return apply_issue_vote(issue_id, 1)


def _adjust_issue_upvotes(db, issue_id, delta):
    if delta:
        db.execute(
            "UPDATE issues SET upvotes=MAX(0, upvotes + ?) WHERE id=?",
            (delta, issue_id),
        )


def apply_issue_vote(issue_id, direction):
    db, uid = get_db(), session["user_id"]
    issue = get_issue_or_404(db, issue_id)
    if not issue:
        return api_error("Issue not found", 404)
    if issue["lifecycle_status"] in ("Resolved", "Verified"):
        return api_error("This issue is already resolved.", 409)

    existing = db.execute(
        "SELECT direction FROM votes WHERE user_id=? AND issue_id=?", (uid, issue_id)
    ).fetchone()
    upvote_delta = 0
    if existing and existing["direction"] == direction:
        db.execute("DELETE FROM votes WHERE user_id=? AND issue_id=?", (uid, issue_id))
        chosen = 0
        if direction == 1:
            upvote_delta = -1
    elif existing:
        db.execute("UPDATE votes SET direction=? WHERE user_id=? AND issue_id=?", (direction, uid, issue_id))
        chosen = direction
        if existing["direction"] == 1:
            upvote_delta = -1
        elif direction == 1:
            upvote_delta = 1
    else:
        db.execute("INSERT INTO votes (user_id, issue_id, direction) VALUES (?,?,?)", (uid, issue_id, direction))
        chosen = direction
        if direction == 1:
            upvote_delta = 1
    _adjust_issue_upvotes(db, issue_id, upvote_delta)
    db.commit()
    issue = reload_and_broadcast(db, issue_id, "votes_changed")
    return jsonify({"upvotes": issue["upvotes"], "downvotes": issue["downvotes"], "my_vote": chosen})


@app.route("/api/issues/<int:issue_id>/vote", methods=["POST"])
@login_required
def api_vote_issue(issue_id):
    data = request.get_json(silent=True) or {}
    value = data.get("direction")
    direction = {"up": 1, "down": -1, "none": 0, 1: 1, -1: -1, 0: 0}.get(value)
    if direction is None:
        return api_error("Choose an upvote, downvote, or remove your vote.")
    if direction == 0:
        db, uid = get_db(), session["user_id"]
        issue = get_issue_or_404(db, issue_id)
        if not issue:
            return api_error("Issue not found", 404)
        existing = db.execute(
            "SELECT direction FROM votes WHERE user_id=? AND issue_id=?", (uid, issue_id)
        ).fetchone()
        if existing:
            db.execute("DELETE FROM votes WHERE user_id=? AND issue_id=?", (uid, issue_id))
            _adjust_issue_upvotes(db, issue_id, -1 if existing["direction"] == 1 else 0)
        db.commit()
        issue = reload_and_broadcast(db, issue_id, "votes_changed")
        return jsonify({"upvotes": issue["upvotes"], "downvotes": issue["downvotes"], "my_vote": 0})
    return apply_issue_vote(issue_id, direction)


@app.route("/api/issues/<int:issue_id>/comments", methods=["GET", "POST"])
@login_required
def api_issue_comments(issue_id):
    db = get_db()
    if not get_issue_or_404(db, issue_id):
        return api_error("Issue not found", 404)
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        body = str(data.get("body", "")).strip()
        if not body or len(body) > 2000:
            return api_error("Comments must be between 1 and 2000 characters.")
        cur = db.execute(
            "INSERT INTO issue_comments (issue_id,user_id,body,created_at) VALUES (?,?,?,?)",
            (issue_id, session["user_id"], body, now_iso()),
        )
        award_xp(db, session["user_id"], issue_id, "comment", 2)
        db.commit()
        comment = db.execute(
            """SELECT c.id,c.body,c.created_at,u.id AS user_id,u.name,u.portal_role,u.role
               FROM issue_comments c JOIN users u ON u.id=c.user_id WHERE c.id=?""",
            (cur.lastrowid,),
        ).fetchone()
        return jsonify(dict(comment)), 201
    rows = db.execute(
        """SELECT c.id,c.body,c.created_at,u.id AS user_id,u.name,u.portal_role,u.role
           FROM issue_comments c JOIN users u ON u.id=c.user_id
           WHERE c.issue_id=? ORDER BY c.id""",
        (issue_id,),
    ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.route("/api/issues/<int:issue_id>/follow", methods=["POST"])
@login_required
def api_follow_issue(issue_id):
    db, uid = get_db(), session["user_id"]
    if not get_issue_or_404(db, issue_id):
        return api_error("Issue not found", 404)
    exists = db.execute(
        "SELECT 1 FROM issue_follows WHERE user_id=? AND issue_id=?", (uid, issue_id)
    ).fetchone()
    if exists:
        db.execute("DELETE FROM issue_follows WHERE user_id=? AND issue_id=?", (uid, issue_id))
        following = False
    else:
        db.execute("INSERT INTO issue_follows (user_id,issue_id,created_at) VALUES (?,?,?)",
                   (uid, issue_id, now_iso()))
        following = True
    db.commit()
    return jsonify({"following": following})


@app.route("/api/politicians")
@login_required
def api_politicians():
    rows = get_db().execute(
        """SELECT u.id,u.name,u.politician_title,u.politician_constituency,u.civic_xp,
                  (SELECT COUNT(*) FROM politician_follows f WHERE f.politician_id=u.id) AS followers_count,
                  EXISTS(SELECT 1 FROM politician_follows f WHERE f.politician_id=u.id AND f.user_id=?) AS followed_by_me
           FROM users u WHERE u.portal_role='politician' AND u.politician_status='approved'
           ORDER BY u.name""",
        (session["user_id"],),
    ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.route("/api/politicians/<int:politician_id>/follow", methods=["POST"])
@login_required
def api_follow_politician(politician_id):
    db, uid = get_db(), session["user_id"]
    politician = db.execute(
        """SELECT 1 FROM users WHERE id=? AND portal_role='politician'
           AND politician_status='approved'""",
        (politician_id,),
    ).fetchone()
    if not politician:
        return api_error("Verified politician not found.", 404)
    if uid == politician_id:
        return api_error("You cannot follow your own profile.", 400)
    existing = db.execute(
        "SELECT 1 FROM politician_follows WHERE user_id=? AND politician_id=?", (uid, politician_id)
    ).fetchone()
    if existing:
        db.execute("DELETE FROM politician_follows WHERE user_id=? AND politician_id=?", (uid, politician_id))
        following = False
    else:
        db.execute(
            "INSERT INTO politician_follows (user_id,politician_id,created_at) VALUES (?,?,?)",
            (uid, politician_id, now_iso()),
        )
        following = True
    db.commit()
    return jsonify({"following": following})


@app.route("/api/politicians/pending")
@login_required
@admin_required
def api_pending_politicians():
    rows = get_db().execute(
        """SELECT id,name,email,politician_title,politician_constituency,created_at FROM users
           WHERE portal_role='politician' AND politician_status='pending'
           ORDER BY created_at"""
    ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.route("/api/politicians/<int:politician_id>/review", methods=["POST"])
@login_required
@admin_required
def api_review_politician(politician_id):
    db, data = get_db(), request.get_json(silent=True) or {}
    decision = data.get("decision")
    if decision not in ("approve", "reject"):
        return api_error("Choose approve or reject.")
    politician = db.execute(
        "SELECT * FROM users WHERE id=? AND portal_role='politician' AND politician_status='pending'",
        (politician_id,),
    ).fetchone()
    if not politician:
        return api_error("Pending politician account not found.", 404)
    db.execute(
        "UPDATE users SET politician_status=? WHERE id=?",
        ("approved" if decision == "approve" else "rejected", politician_id),
    )
    db.commit()
    return jsonify({"id": politician_id, "status": "approved" if decision == "approve" else "rejected"})


@app.route("/api/issues/<int:issue_id>/lifecycle", methods=["POST"])
@login_required
def api_update_lifecycle(issue_id):
    db, data = get_db(), request.get_json(silent=True) or {}
    target = data.get("status")
    if target not in LIFECYCLE_STATUSES:
        return api_error("Choose a valid issue lifecycle status.")
    issue = get_issue_or_404(db, issue_id)
    if not issue:
        return api_error("Issue not found", 404)
    current = issue["lifecycle_status"] or "Reported"
    transitions = dict(zip(LIFECYCLE_STATUSES, LIFECYCLE_STATUSES[1:]))
    uid, role = session["user_id"], session["role"]
    assigned_politician_id = issue["concerned_politician_id"]

    if target == "Verified":
        if current != "Resolved" or issue["reporter_id"] != uid:
            return api_error("Only the reporting citizen can verify a resolved issue.", 403)
    else:
        if transitions.get(current) != target:
            return api_error(f"Issue must move one step forward from {current}.", 409)
        if role == "politician":
            if issue["claimed_by"] is not None:
                return api_error("A worker currently holds this repair; the worker lifecycle takes precedence.", 409)
            if assigned_politician_id not in (None, uid):
                return api_error("This issue is assigned to another politician.", 403)
            if assigned_politician_id is None and current != "Reported":
                return api_error("An administrator must assign the politician before review continues.", 403)
            if assigned_politician_id is None:
                assigned_politician_id = uid
        elif role == "worker":
            if target != "In Progress" or issue["claimed_by"] != uid:
                return api_error("Workers may only start a job they currently hold.", 403)
        elif role == "admin":
            requested_politician = data.get("concerned_politician_id")
            if requested_politician not in (None, ""):
                try:
                    assigned_politician_id = int(requested_politician)
                except (TypeError, ValueError):
                    return api_error("Choose a valid politician.")
                active = db.execute(
                    """SELECT 1 FROM users WHERE id=? AND portal_role='politician'
                       AND politician_status='approved'""",
                    (assigned_politician_id,),
                ).fetchone()
                if not active:
                    return api_error("Choose an active approved politician.")
            if target == "Assigned" and assigned_politician_id is None:
                return api_error("Assign this issue to an approved politician before advancing.")
        else:
            return api_error("Only the assigned politician or an administrator can update this stage.", 403)

    now = now_iso()
    cur = db.execute(
        """UPDATE issues SET lifecycle_status=?,concerned_politician_id=?,updated_at=?
           WHERE id=? AND lifecycle_status=?""",
        (target, assigned_politician_id, now, issue_id, current),
    )
    if not cur.rowcount:
        db.rollback()
        return api_error("Issue status changed while you were updating it. Reload and try again.", 409)
    if target == "Resolved":
        db.execute(
            "UPDATE issues SET status='Resolved',resolved_by=?,updated_at=? WHERE id=?",
            (uid, now, issue_id),
        )
        if role == "politician":
            award_xp(db, uid, issue_id, "politician_resolved", 10)
    elif target == "Verified":
        award_xp(db, issue["reporter_id"], issue_id, "verified_report", 20)
        if issue["concerned_politician_id"]:
            award_xp(db, issue["concerned_politician_id"], issue_id, "issue_verified", 25)
    elif target == "Acknowledged" and role == "politician":
        award_xp(db, uid, issue_id, "politician_acknowledged", 5)
    default_note = "Worker started the assigned repair." if target == "In Progress" and role == "worker" \
        else f"Issue moved to {target}."
    log_status(db, issue_id, target, str(data.get("note", "")).strip()[:500] or default_note, now)
    db.commit()
    return jsonify(reload_and_broadcast(db, issue_id))


@app.route("/profile/<int:user_id>")
@login_required
def profile_page(user_id):
    profile = get_db().execute(
        "SELECT id,name,role,portal_role,politician_title,politician_constituency FROM users WHERE id=?",
        (user_id,),
    ).fetchone()
    if not profile:
        abort(404)
    return render_template("profile.html", profile=dict(profile), profile_data=profile_summary(get_db(), user_id))


@app.route("/politicians")
@login_required
def politicians_page():
    return render_template("politicians.html")


def profile_summary(db, user_id):
    user = db.execute(
        "SELECT id,name,role,portal_role,politician_title,politician_constituency,civic_xp "
        "FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if not user:
        return None
    is_politician = user["portal_role"] == "politician"
    reports = db.execute(
        "SELECT COUNT(*) FROM issues WHERE reporter_id=?", (user_id,)
    ).fetchone()[0]
    verified = db.execute(
        "SELECT COUNT(*) FROM issues WHERE reporter_id=? AND lifecycle_status='Verified'", (user_id,)
    ).fetchone()[0]
    month_cursor = date.today().replace(day=1)
    month_keys = []
    for _ in range(6):
        month_keys.append(f"{month_cursor.year:04d}-{month_cursor.month:02d}")
        if month_cursor.month == 1:
            month_cursor = date(month_cursor.year - 1, 12, 1)
        else:
            month_cursor = date(month_cursor.year, month_cursor.month - 1, 1)
    month_keys.reverse()
    monthly_rows = db.execute(
        """SELECT substr(created_at,1,7) AS month,COUNT(*) AS count
           FROM xp_events WHERE user_id=? AND created_at>=? GROUP BY month ORDER BY month""",
        (user_id, month_keys[0] + "-01"),
    ).fetchall()
    monthly_counts = {row["month"]: row["count"] for row in monthly_rows}
    result = {
        "id": user["id"], "name": user["name"], "role": "politician" if is_politician else user["role"],
        "title": user["politician_title"], "constituency": user["politician_constituency"],
        "civic_xp": user["civic_xp"], "reported": reports, "verified": verified,
        "monthly": [{"month": key, "count": monthly_counts.get(key, 0)} for key in month_keys],
        "categories": {},
    }
    if is_politician:
        result["assigned"] = db.execute(
            "SELECT COUNT(*) FROM issues WHERE concerned_politician_id=?", (user_id,)
        ).fetchone()[0]
        result["resolved"] = db.execute(
            "SELECT COUNT(*) FROM issues WHERE concerned_politician_id=? AND lifecycle_status IN ('Resolved','Verified')",
            (user_id,),
        ).fetchone()[0]
        result["verified"] = db.execute(
            "SELECT COUNT(*) FROM issues WHERE concerned_politician_id=? AND lifecycle_status='Verified'",
            (user_id,),
        ).fetchone()[0]
        result["categories"] = {
            row["category"]: row["count"] for row in db.execute(
                """SELECT category,COUNT(*) AS count FROM issues WHERE concerned_politician_id=?
                   GROUP BY category ORDER BY count DESC""",
                (user_id,),
            )
        }
        result["accountability_rate"] = round(
            result["verified"] / result["assigned"] * 100, 1
        ) if result["assigned"] else 0
    else:
        result["categories"] = {
            row["category"]: row["count"] for row in db.execute(
                "SELECT category,COUNT(*) AS count FROM issues WHERE reporter_id=? GROUP BY category ORDER BY count DESC",
                (user_id,),
            )
        }
    return result


@app.route("/api/profiles/<int:user_id>")
@login_required
def api_profile(user_id):
    result = profile_summary(get_db(), user_id)
    if not result:
        return api_error("Profile not found.", 404)
    result["followed_by_me"] = bool(get_db().execute(
        "SELECT 1 FROM politician_follows WHERE user_id=? AND politician_id=?",
        (session["user_id"], user_id),
    ).fetchone()) if result["role"] == "politician" else False
    return jsonify(result)


@app.route("/api/issues/<int:issue_id>/status", methods=["POST"])
@login_required
@admin_required
def api_update_status(issue_id):
    """Manual status control for the ward office — but never at the cost of the worker flow:
    a worker-held job can't be 'resolved' without proof, so the only override is to take
    the job back (set it to Reported), which refunds the escrow."""
    db = get_db()
    new_status = (request.get_json(silent=True) or {}).get("status")
    note = ((request.get_json(silent=True) or {}).get("note") or "").strip()
    if new_status not in ADMIN_STATUSES:
        return api_error("Choose Reported, In Progress or Resolved.")
    issue = get_issue_or_404(db, issue_id)
    if not issue:
        return api_error("Issue not found", 404)
    if issue["status"] == "Resolved":
        return api_error("Resolved issues are final.", 409)

    now = now_iso()
    if issue["status"] == "Claimed":
        if new_status != "Reported":
            return api_error("A worker holds this job. Set it to Reported to take it back, "
                             "or wait for the worker to submit proof.", 409)
        release_claim(db, issue, "Job taken back by the ward office — reopened for claiming.", now)
    elif new_status != issue["status"]:
        lifecycle_for_status = {
            "Reported": "Reported", "In Progress": "In Progress", "Resolved": "Resolved"
        }[new_status]
        if lifecycle_for_status != issue["lifecycle_status"]:
            return api_error("Advance the civic lifecycle one stage at a time before changing this status.", 409)
        db.execute("UPDATE issues SET status=?, updated_at=? WHERE id=?", (new_status, now, issue_id))
        log_status(db, issue_id, new_status, note or f"Status set to {new_status} by the ward office.", now)
    db.commit()
    return jsonify(reload_and_broadcast(db, issue_id))


# ---------------------------------------------------------------------------
# API — worker job lifecycle
# ---------------------------------------------------------------------------
@app.route("/api/issues/<int:issue_id>/claim", methods=["POST"])
@login_required
@worker_required
def api_claim_issue(issue_id):
    db, uid, now = get_db(), session["user_id"], now_iso()
    issue = get_issue_or_404(db, issue_id)
    worker = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not issue:
        return api_error("Issue not found", 404)
    if issue["category"] not in SKILL_CATEGORY_MAP[worker["skill"]]:
        return api_error(f"This is a '{issue['category']}' job — it doesn't match your skill ({worker['skill']}).", 403)
    if worker["lat"] is None:
        return api_error("Set your work location first so we can check the job is near you.")
    if issue["lifecycle_status"] != "Reported":
        return api_error("This issue is already being reviewed or assigned.", 409)
    distance = haversine_km(worker["lat"], worker["lng"], issue["lat"], issue["lng"])
    if distance > worker["radius_km"]:
        return api_error(f"This job is {distance:.1f} km away — outside your {worker['radius_km']:g} km service area.", 403)
    active = db.execute("SELECT COUNT(*) FROM issues WHERE claimed_by=? AND status='Claimed'", (uid,)).fetchone()[0]
    if active >= MAX_ACTIVE_CLAIMS:
        return api_error(f"You already hold {MAX_ACTIVE_CLAIMS} active jobs — finish or release one first.", 409)

    sponsor = best_sponsor(db.execute("SELECT * FROM sponsors WHERE active=1").fetchall(), issue["category"])
    if not sponsor:
        return api_error("No sponsor is funding this category right now, so the job can't be claimed yet.", 409)

    try:
        # 1) reserve the reward: the budget check lives in the WHERE clause, so two workers can
        #    never reserve the same last rupees; 2) take the lock: only succeeds if still open.
        #    Both are in one transaction — if either fails, neither happens.
        if not db.execute("UPDATE sponsors SET budget_remaining = budget_remaining - ? "
                          "WHERE id=? AND active=1 AND budget_remaining >= ?",
                          (sponsor["reward_per_issue"], sponsor["id"], sponsor["reward_per_issue"])).rowcount:
            raise LookupError("The sponsor's budget was just used up — please try another job.")
        if not db.execute("UPDATE issues SET status='Claimed', lifecycle_status='Assigned', claimed_by=?, "
                          "claimed_at=?, reward_amount=?, reward_sponsor_id=?, updated_at=? "
                          "WHERE id=? AND status='Reported' AND claimed_by IS NULL AND lifecycle_status='Reported'",
                          (uid, now, sponsor["reward_per_issue"], sponsor["id"], now, issue_id)).rowcount:
            raise LookupError("This job was just taken by someone else.")
        log_status(db, issue_id, "Claimed",
                   f"Claimed by {worker['name']} ({worker['skill']}) — ₹{sponsor['reward_per_issue']:.0f} reserved "
                   f"from {sponsor['name']}.", now)
        log_status(db, issue_id, "Assigned", f"Assigned to worker {worker['name']}.", now)
        db.commit()
    except LookupError as e:
        db.rollback()
        return api_error(str(e), 409)
    return jsonify(reload_and_broadcast(db, issue_id))


@app.route("/api/issues/<int:issue_id>/release", methods=["POST"])
@login_required
@worker_required
def api_release_issue(issue_id):
    db = get_db()
    issue = get_issue_or_404(db, issue_id)
    if not issue:
        return api_error("Issue not found", 404)
    # Only an ACTIVE claim can be released. (Releasing a paid, resolved job would re-open it
    # while keeping the money — the classic way to be paid twice.)
    if issue["claimed_by"] != session["user_id"] or issue["status"] != "Claimed":
        return api_error("You can only release a job you currently hold.", 403)
    release_claim(db, issue, "Worker released the job — reopened for claiming, reward returned to the sponsor.", now_iso())
    db.commit()
    return jsonify(reload_and_broadcast(db, issue_id))


@app.route("/api/issues/<int:issue_id>/resolve", methods=["POST"])
@login_required
@worker_required
def api_resolve_issue(issue_id):
    db, uid, now = get_db(), session["user_id"], now_iso()
    issue = get_issue_or_404(db, issue_id)
    if not issue:
        return api_error("Issue not found", 404)
    if issue["claimed_by"] != uid or issue["status"] != "Claimed":
        return api_error("You can only resolve a job you currently hold.", 403)

    note = request.form.get("note", "").strip()
    photo = request.files.get("photo")
    if not note:
        return api_error("Please describe the work you did.")
    if not photo or not photo.filename:
        return api_error("A photo of the completed work is required.")
    try:
        proof = save_photo(photo)
    except ValueError as e:
        return api_error(str(e))

    verdict, score, reasons = verify_completion_photo(photo_stats(issue["photo_path"]), photo_stats(proof))
    delete_photo(issue["completion_photo_path"])        # a resubmission replaces the old proof

    if issue["lifecycle_status"] == "Assigned":
        log_status(db, issue_id, "In Progress", "Worker started the assigned repair.", now)
    db.execute("""UPDATE issues SET status='Resolved', lifecycle_status='Resolved', resolved_by=?,
                  resolution_note=?, completion_photo_path=?, verification_status=?, verification_notes=?,
                  verification_score=?, updated_at=? WHERE id=?""",
               (uid, note, proof, verdict, "; ".join(reasons), score, now, issue_id))
    if verdict == "auto_approved":
        amount = pay_reward(db, issue_id, uid, now)
        log_status(db, issue_id, "Resolved", f"{note} Photo check passed ({score}/100) — ₹{amount:.0f} paid.", now)
    else:
        log_status(db, issue_id, "Resolved",
                   f"{note} Photo flagged for ward review ({'; '.join(reasons)}) — reward on hold.", now)
    if verdict == "auto_approved":
        award_xp(db, issue["reporter_id"], issue_id, "resolved_report", 8)
    db.commit()
    return jsonify(reload_and_broadcast(db, issue_id))


# ---------------------------------------------------------------------------
# API — admin review of flagged completions
# ---------------------------------------------------------------------------
@app.route("/api/reviews")
@login_required
@admin_required
def api_list_reviews():
    db = get_db()
    rows = fetch_issues(db, " WHERE i.verification_status = 'pending_review' ORDER BY i.updated_at")
    return jsonify([serialize_issue(r) for r in rows])


def pending_issue_or_error(db, issue_id):
    issue = get_issue_or_404(db, issue_id)
    if not issue:
        return None, api_error("Issue not found", 404)
    if issue["verification_status"] != "pending_review":
        return None, api_error("This issue isn't awaiting review.", 409)
    return issue, None


@app.route("/api/issues/<int:issue_id>/approve-reward", methods=["POST"])
@login_required
@admin_required
def api_approve_reward(issue_id):
    db, now = get_db(), now_iso()
    issue, err = pending_issue_or_error(db, issue_id)
    if err:
        return err
    amount = pay_reward(db, issue_id, issue["resolved_by"], now)
    db.execute("UPDATE issues SET verification_status='approved', updated_at=? WHERE id=?", (now, issue_id))
    log_status(db, issue_id, "Resolved", f"Ward office approved the proof photo — ₹{amount:.0f} paid.", now)
    db.commit()
    return jsonify(reload_and_broadcast(db, issue_id))


@app.route("/api/issues/<int:issue_id>/reject-reward", methods=["POST"])
@login_required
@admin_required
def api_reject_reward(issue_id):
    """Send the job back to the SAME worker (their lock and escrow stay intact) so a
    honest worker with a bad photo can retake it rather than be dead-ended."""
    db, now = get_db(), now_iso()
    issue, err = pending_issue_or_error(db, issue_id)
    if err:
        return err
    reason = ((request.get_json(silent=True) or {}).get("note") or "").strip() or "Please retake a clearer photo of the finished work."
    delete_photo(issue["completion_photo_path"])
    db.execute("""UPDATE issues SET status='Claimed', lifecycle_status='Assigned', resolved_by=NULL, resolution_note=NULL,
                  completion_photo_path=NULL, verification_status='rejected',
                  verification_notes=?, updated_at=? WHERE id=?""", (reason, now, issue_id))
    log_status(db, issue_id, "Claimed", f"Ward office rejected the proof photo: {reason}", now)
    db.commit()
    return jsonify(reload_and_broadcast(db, issue_id))


# ---------------------------------------------------------------------------
# API — worker dashboard
# ---------------------------------------------------------------------------
def worker_board(db, uid, category=None):
    """Open jobs this worker may actually take: matching skill, inside their service
    radius, and funded. Returns (jobs, number_waiting_for_funding), or None if the worker
    hasn't set a location yet."""
    me = db.execute("SELECT skill, lat, lng, radius_km FROM users WHERE id=?", (uid,)).fetchone()
    if me["lat"] is None:
        return None
    allowed = [c for c in SKILL_CATEGORY_MAP[me["skill"]] if not category or c == category]
    if not allowed:
        return [], 0

    # cheap bounding-box filter in SQL first, exact great-circle distance only on what's left
    dlat = me["radius_km"] / 111.0
    dlng = me["radius_km"] / (111.0 * max(math.cos(math.radians(me["lat"])), 0.01))
    marks = ",".join("?" * len(allowed))
    rows = fetch_issues(db, f" WHERE i.status='Reported' AND i.lifecycle_status='Reported' "
                            f"AND i.claimed_by IS NULL AND i.category IN ({marks}) "
                            "AND i.lat BETWEEN ? AND ? AND i.lng BETWEEN ? AND ?",
                        (*allowed, me["lat"] - dlat, me["lat"] + dlat, me["lng"] - dlng, me["lng"] + dlng))
    quotes = reward_quotes(db)
    jobs, unfunded = [], 0
    for r in rows:
        distance = haversine_km(me["lat"], me["lng"], r["lat"], r["lng"])
        if distance > me["radius_km"]:
            continue
        job = serialize_issue(r, quotes)
        if not job["estimated_reward"]:
            unfunded += 1
            continue
        job["distance_km"] = round(distance, 2)
        jobs.append(job)
    jobs.sort(key=lambda j: (-j["impact_score"], j["distance_km"]))   # most urgent first
    return jobs, unfunded


@app.route("/api/worker/jobs")
@login_required
@worker_required
def api_worker_jobs():
    board = worker_board(get_db(), session["user_id"], request.args.get("category") or None)
    if board is None:
        return jsonify({"has_location": False, "jobs": [], "waiting_for_funding": 0})
    return jsonify({"has_location": True, "jobs": board[0], "waiting_for_funding": board[1]})


@app.route("/api/worker/my-jobs")
@login_required
@worker_required
def api_worker_my_jobs():
    db, uid = get_db(), session["user_id"]
    me = db.execute("SELECT lat, lng FROM users WHERE id=?", (uid,)).fetchone()
    jobs = []
    for r in fetch_issues(db, " WHERE i.claimed_by = ? AND i.status = 'Claimed' ORDER BY i.claimed_at DESC", (uid,)):
        job = serialize_issue(r)
        if me["lat"] is not None:
            job["distance_km"] = round(haversine_km(me["lat"], me["lng"], r["lat"], r["lng"]), 2)
        jobs.append(job)
    return jsonify(jobs)


@app.route("/api/worker/summary")
@login_required
@worker_required
def api_worker_summary():
    db, uid = get_db(), session["user_id"]
    user = db.execute("SELECT wallet_balance, skill FROM users WHERE id=?", (uid,)).fetchone()
    count = lambda sql: db.execute(sql, (uid,)).fetchone()[0]
    board = worker_board(db, uid)
    return jsonify({
        "wallet_balance": user["wallet_balance"],
        "skill": user["skill"],
        "has_location": board is not None,
        "available_jobs": len(board[0]) if board else 0,
        "active_jobs": count("SELECT COUNT(*) FROM issues WHERE claimed_by=? AND status='Claimed'"),
        "resolved_jobs": count("SELECT COUNT(*) FROM issues WHERE resolved_by=? AND status='Resolved'"),
    })


@app.route("/api/worker/location", methods=["GET", "POST"])
@login_required
@worker_required
def api_worker_location():
    db, uid = get_db(), session["user_id"]
    me = db.execute("SELECT lat, lng, radius_km, location_label FROM users WHERE id=?", (uid,)).fetchone()
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        try:
            lat = float(data["lat"]) if data.get("lat") is not None else me["lat"]
            lng = float(data["lng"]) if data.get("lng") is not None else me["lng"]
            radius = float(data["radius_km"]) if data.get("radius_km") is not None else me["radius_km"]
        except (TypeError, ValueError):
            return api_error("Location and radius must be numbers.")
        if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return api_error("That location looks invalid.")
        label = str(data["label"]).strip()[:200] if data.get("label") is not None else (me["location_label"] or "")
        db.execute("UPDATE users SET lat=?, lng=?, radius_km=?, location_label=? WHERE id=?",
                   (lat, lng, max(1.0, min(100.0, radius)), label, uid))
        db.commit()
        me = db.execute("SELECT lat, lng, radius_km, location_label FROM users WHERE id=?", (uid,)).fetchone()
    return jsonify({"lat": me["lat"], "lng": me["lng"], "radius_km": me["radius_km"], "label": me["location_label"] or ""})


@app.route("/api/worker/pending-review")
@login_required
@worker_required
def api_worker_pending_review():
    rows = fetch_issues(get_db(), " WHERE i.resolved_by = ? AND i.verification_status = 'pending_review' "
                                  "ORDER BY i.updated_at DESC", (session["user_id"],))
    return jsonify([serialize_issue(r) for r in rows])


@app.route("/api/worker/history")
@login_required
@worker_required
def api_worker_history():
    rows = get_db().execute(
        """SELECT rl.amount, rl.created_at, i.id AS issue_id, i.title, i.category, s.name AS sponsor_name
           FROM reward_ledger rl JOIN issues i ON i.id = rl.issue_id JOIN sponsors s ON s.id = rl.sponsor_id
           WHERE rl.worker_id = ? ORDER BY rl.id DESC""", (session["user_id"],)).fetchall()
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# API — statistics, analytics, hotspots, export
# ---------------------------------------------------------------------------
@app.route("/api/stats")
@login_required
def api_stats():
    db = get_db()
    by_status = {r["status"]: r["c"] for r in db.execute("SELECT status, COUNT(*) c FROM issues GROUP BY status")}
    total = sum(by_status.values())
    resolved = by_status.get("Resolved", 0)
    return jsonify({
        "total": total,
        "reported": by_status.get("Reported", 0),
        "claimed": by_status.get("Claimed", 0),
        "in_progress": by_status.get("In Progress", 0),
        "resolved": resolved,
        "high_priority_open": db.execute(
            "SELECT COUNT(*) FROM issues WHERE priority='High' AND status!='Resolved'").fetchone()[0],
        "resolution_rate": round(resolved / total * 100, 1) if total else 0,
    })


@app.route("/api/analytics")
@login_required
@admin_required
def api_analytics():
    db = get_db()
    by_category = {r["category"]: r["c"] for r in db.execute(
        "SELECT category, COUNT(*) c FROM issues GROUP BY category ORDER BY c DESC")}
    by_status = {s: 0 for s in LIFECYCLE_STATUSES}
    by_status.update({
        r["lifecycle_status"]: r["c"]
        for r in db.execute("SELECT lifecycle_status, COUNT(*) c FROM issues GROUP BY lifecycle_status")
    })

    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    per_day = {r["d"]: r["c"] for r in db.execute(
        "SELECT substr(created_at,1,10) d, COUNT(*) c FROM issues WHERE created_at >= ? GROUP BY d", (days[0],))}
    return jsonify({"by_category": by_category, "by_status": by_status,
                    "trend": [{"date": d, "count": per_day.get(d, 0)} for d in days]})


@app.route("/api/hotspots")
@login_required
@admin_required
def api_hotspots():
    rows = get_db().execute("SELECT id, lat, lng, status, category, address FROM issues").fetchall()
    return jsonify(detect_hotspots([dict(r) for r in rows]))


def csv_safe(value):
    """Titles and addresses are typed by the public; a cell starting with = + - @ would be
    executed as a formula when an admin opens the export in Excel. A leading ' defuses it."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@app.route("/api/export/csv")
@login_required
@admin_required
def api_export_csv():
    rows = get_db().execute(
        """SELECT i.*, w.name AS worker_name FROM issues i
           LEFT JOIN users w ON w.id = i.resolved_by ORDER BY i.created_at DESC""").fetchall()
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(["ID", "Title", "Category", "Status", "Priority", "Upvotes", "Latitude", "Longitude",
                  "Address", "Reported At", "Resolved By", "Reward Paid"])
    for r in rows:
        out.writerow([r["id"], csv_safe(r["title"]), r["category"], r["status"], r["priority"], r["upvotes"],
                      r["lat"], r["lng"], csv_safe(r["address"]), r["created_at"],
                      csv_safe(r["worker_name"]), r["reward_amount"] if r["reward_paid"] else 0])
    return send_file(io.BytesIO(buf.getvalue().encode("utf-8")), mimetype="text/csv",
                     as_attachment=True, download_name="urbanpulse_issues.csv")


# ---------------------------------------------------------------------------
# API — sponsors & rewards oversight (admin)
# ---------------------------------------------------------------------------
def get_sponsor(db, sponsor_id):
    return db.execute("SELECT * FROM sponsors WHERE id=?", (sponsor_id,)).fetchone()


@app.route("/api/sponsors")
@login_required
@admin_required
def api_list_sponsors():
    rows = get_db().execute("SELECT * FROM sponsors ORDER BY active DESC, budget_remaining DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/sponsors", methods=["POST"])
@login_required
@admin_required
def api_create_sponsor():
    db, data = get_db(), request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:100]
    category = data.get("category") or "All"
    try:
        reward, budget = float(data.get("reward_per_issue")), float(data.get("budget_total"))
    except (TypeError, ValueError):
        return api_error("Reward and budget must be numbers.")
    if not name or reward <= 0 or budget < reward:
        return api_error("Give a name, a positive reward, and a budget of at least one reward.")
    if category != "All" and category not in CATEGORIES:
        return api_error("Invalid category.")
    cur = db.execute("""INSERT INTO sponsors (name, category, reward_per_issue, budget_total, budget_remaining, created_at)
                        VALUES (?,?,?,?,?,?)""", (name, category, reward, budget, budget, now_iso()))
    db.commit()
    return jsonify(dict(get_sponsor(db, cur.lastrowid))), 201


@app.route("/api/sponsors/<int:sponsor_id>/toggle", methods=["POST"])
@login_required
@admin_required
def api_toggle_sponsor(sponsor_id):
    db = get_db()
    if not get_sponsor(db, sponsor_id):
        return api_error("Sponsor not found", 404)
    db.execute("UPDATE sponsors SET active = 1 - active WHERE id=?", (sponsor_id,))
    db.commit()
    return jsonify(dict(get_sponsor(db, sponsor_id)))


@app.route("/api/sponsors/<int:sponsor_id>/topup", methods=["POST"])
@login_required
@admin_required
def api_topup_sponsor(sponsor_id):
    db = get_db()
    try:
        amount = float((request.get_json(silent=True) or {}).get("amount"))
    except (TypeError, ValueError):
        return api_error("Amount must be a number.")
    if amount <= 0:
        return api_error("Top-up must be positive.")
    if not get_sponsor(db, sponsor_id):
        return api_error("Sponsor not found", 404)
    db.execute("UPDATE sponsors SET budget_total = budget_total + ?, budget_remaining = budget_remaining + ? WHERE id=?",
               (amount, amount, sponsor_id))
    db.commit()
    return jsonify(dict(get_sponsor(db, sponsor_id)))


@app.route("/api/rewards-summary")
@login_required
@admin_required
def api_rewards_summary():
    db = get_db()
    one = lambda sql: db.execute(sql).fetchone()[0]
    leaderboard = db.execute(
        """SELECT u.name, u.skill, COALESCE(SUM(rl.amount), 0) AS earned, COUNT(rl.id) AS jobs
           FROM users u JOIN reward_ledger rl ON rl.worker_id = u.id
           GROUP BY u.id ORDER BY earned DESC LIMIT 6""").fetchall()
    return jsonify({
        "paid": one("SELECT COALESCE(SUM(amount),0) FROM reward_ledger"),
        # money promised to workers on live jobs but not yet paid out
        "reserved": one("SELECT COALESCE(SUM(reward_amount),0) FROM issues "
                        "WHERE reward_sponsor_id IS NOT NULL AND reward_paid = 0"),
        "available": one("SELECT COALESCE(SUM(budget_remaining),0) FROM sponsors"),
        "jobs_done": one("SELECT COUNT(*) FROM reward_ledger"),
        "leaderboard": [dict(r) for r in leaderboard],
    })


# ---------------------------------------------------------------------------
# Live stream
# ---------------------------------------------------------------------------
@app.route("/stream")
@login_required
def stream():
    def events():
        q = queue.Queue(maxsize=50)
        with _subscribers_lock:
            _subscribers.append(q)
        try:
            yield "event: connected\ndata: {}\n\n"
            while True:
                try:
                    yield f"data: {q.get(timeout=25)}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"      # keeps proxies from closing an idle connection
        finally:
            with _subscribers_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    return Response(events(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    database.init_db(seed_demo_data=True)
    print("UrbanPulse running at http://127.0.0.1:5000")
    print("  admin   admin@urbanpulse.gov     / Admin@123")
    print("  citizen citizen@urbanpulse.gov   / Citizen@123")
    print("  workers worker@ (electrician) | plumber@ | roadcrew@urbanpulse.gov  / Worker@123")
    # Browsers only allow GPS on localhost or HTTPS. URBANPULSE_HTTPS=1 (needs `pip install cryptography`)
    # serves a self-signed certificate so a phone on the same network can use "current location".
    https = os.environ.get("URBANPULSE_HTTPS") == "1"
    app.run(host=os.environ.get("URBANPULSE_HOST", "127.0.0.1"), port=5000, threaded=True,
            ssl_context="adhoc" if https else None)
