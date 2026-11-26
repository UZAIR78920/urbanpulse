"""
UrbanPulse — database.py
SQLite data layer (standard library only, no ORM).

Tables
  users          one table for all three roles (citizen | worker | admin);
                 worker-only columns (skill, wallet, base location) are NULL otherwise
  issues         one row per reported problem, including its claim / resolution /
                 reward state, so a job's whole lifecycle is readable in one place
  status_log     append-only history shown as the timeline on each issue
  votes          one row per (user, issue): the primary key enforces one vote each
  sponsors       companies funding rewards for a category (or 'All')
  reward_ledger  one row per payout actually made (audit trail of money moved)

Money flow (see app.py): claim -> sponsor budget is ESCROWED on the issue
                         resolve + photo verified -> escrow is PAID into the worker's wallet
                         release -> escrow is REFUNDED to the sponsor
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

from intelligence import classify_priority

DB_PATH = os.environ.get(
    "URBANPULSE_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "urbanpulse.db")
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    email          TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'citizen' CHECK (role IN ('citizen','worker','admin')),
    skill          TEXT,                              -- worker only
    wallet_balance REAL NOT NULL DEFAULT 0,           -- worker only: total rewards received
    lat            REAL,                              -- worker only: base location ...
    lng            REAL,
    radius_km      REAL NOT NULL DEFAULT 10,          -- ... and how far they will travel
    location_label TEXT,
    created_at     TEXT NOT NULL,
    portal_role    TEXT NOT NULL DEFAULT 'citizen',
    politician_title TEXT,
    politician_constituency TEXT,
    politician_status TEXT NOT NULL DEFAULT 'none',
    official_email_verified INTEGER NOT NULL DEFAULT 0,
    verification_token_hash TEXT,
    verification_expires_at TEXT,
    civic_xp       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS issues (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    title                 TEXT NOT NULL,
    description           TEXT NOT NULL,
    category              TEXT NOT NULL,
    lat                   REAL NOT NULL,
    lng                   REAL NOT NULL,
    address               TEXT,
    photo_path            TEXT,                       -- citizen's original photo
    status                TEXT NOT NULL DEFAULT 'Reported'
                          CHECK (status IN ('Reported','Claimed','In Progress','Resolved')),
    priority              TEXT NOT NULL DEFAULT 'Low' CHECK (priority IN ('Low','Medium','High')),
    upvotes               INTEGER NOT NULL DEFAULT 0,
    reporter_id           INTEGER NOT NULL REFERENCES users(id),
    claimed_by            INTEGER REFERENCES users(id),
    claimed_at            TEXT,
    resolved_by           INTEGER REFERENCES users(id),
    resolution_note       TEXT,
    completion_photo_path TEXT,                       -- worker's proof of work
    verification_status   TEXT,                       -- NULL | auto_approved | pending_review | approved | rejected
    verification_notes    TEXT,
    verification_score    REAL,
    reward_amount         REAL NOT NULL DEFAULT 0,    -- amount locked (escrowed) for this job at claim time
    reward_sponsor_id     INTEGER REFERENCES sponsors(id),
    reward_paid           INTEGER NOT NULL DEFAULT 0, -- 1 once the escrow reached the worker's wallet
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    category_group        TEXT,
    subcategory           TEXT,
    severity              TEXT NOT NULL DEFAULT 'Medium',
    concerned_politician_id INTEGER REFERENCES users(id),
    lifecycle_status      TEXT NOT NULL DEFAULT 'Reported',
    reported_date         TEXT
);

CREATE TABLE IF NOT EXISTS votes (
    user_id  INTEGER NOT NULL REFERENCES users(id),
    issue_id INTEGER NOT NULL REFERENCES issues(id),
    direction INTEGER NOT NULL DEFAULT 1 CHECK (direction IN (-1, 1)),
    PRIMARY KEY (user_id, issue_id)
);

CREATE TABLE IF NOT EXISTS issue_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id   INTEGER NOT NULL REFERENCES issues(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issue_follows (
    user_id    INTEGER NOT NULL REFERENCES users(id),
    issue_id   INTEGER NOT NULL REFERENCES issues(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, issue_id)
);

CREATE TABLE IF NOT EXISTS politician_follows (
    user_id       INTEGER NOT NULL REFERENCES users(id),
    politician_id INTEGER NOT NULL REFERENCES users(id),
    created_at    TEXT NOT NULL,
    PRIMARY KEY (user_id, politician_id),
    CHECK (user_id != politician_id)
);

CREATE TABLE IF NOT EXISTS issue_media (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id   INTEGER NOT NULL REFERENCES issues(id),
    media_type TEXT NOT NULL CHECK (media_type IN ('image','video')),
    file_path  TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS xp_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    issue_id   INTEGER REFERENCES issues(id),
    event_type TEXT NOT NULL,
    points     INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, issue_id, event_type)
);

CREATE TABLE IF NOT EXISTS status_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id   INTEGER NOT NULL REFERENCES issues(id),
    status     TEXT NOT NULL,
    note       TEXT,
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sponsors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    category         TEXT NOT NULL,                   -- a category, or 'All'
    reward_per_issue REAL NOT NULL CHECK (reward_per_issue > 0),
    budget_total     REAL NOT NULL,
    budget_remaining REAL NOT NULL CHECK (budget_remaining >= 0),  -- DB itself refuses overspending
    active           INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reward_ledger (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id   INTEGER NOT NULL REFERENCES issues(id),
    worker_id  INTEGER NOT NULL REFERENCES users(id),
    sponsor_id INTEGER NOT NULL REFERENCES sponsors(id),
    amount     REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_issues_status   ON issues(status);
CREATE INDEX IF NOT EXISTS idx_issues_reporter ON issues(reporter_id);
CREATE INDEX IF NOT EXISTS idx_issues_claimed  ON issues(claimed_by);
CREATE INDEX IF NOT EXISTS idx_log_issue       ON status_log(issue_id);
CREATE INDEX IF NOT EXISTS idx_ledger_worker   ON reward_ledger(worker_id);
CREATE INDEX IF NOT EXISTS idx_comments_issue ON issue_comments(issue_id, created_at);
CREATE INDEX IF NOT EXISTS idx_media_issue ON issue_media(issue_id);
CREATE INDEX IF NOT EXISTS idx_xp_user ON xp_events(user_id, created_at);
"""


def now_iso():
    """UTC timestamp, second precision, stored as text (sorts correctly as a string)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(seed_demo_data=True):
    fresh = not os.path.exists(DB_PATH)
    conn = get_db()
    conn.execute("PRAGMA journal_mode = WAL")  # readers (SSE, dashboards) don't block writers
    conn.executescript(SCHEMA)
    _upgrade_schema(conn)
    if fresh and seed_demo_data:
        _seed(conn)
    conn.commit()
    conn.close()


def _upgrade_schema(conn):
    """Additive upgrades keep existing demo/user databases usable."""
    columns = {
        "users": {
            "portal_role": "TEXT NOT NULL DEFAULT 'citizen'",
            "politician_title": "TEXT",
            "politician_constituency": "TEXT",
            "politician_status": "TEXT NOT NULL DEFAULT 'none'",
            "official_email_verified": "INTEGER NOT NULL DEFAULT 0",
            "verification_token_hash": "TEXT",
            "verification_expires_at": "TEXT",
            "civic_xp": "INTEGER NOT NULL DEFAULT 0",
        },
        "issues": {
            "category_group": "TEXT",
            "subcategory": "TEXT",
            "severity": "TEXT NOT NULL DEFAULT 'Medium'",
            "concerned_politician_id": "INTEGER REFERENCES users(id)",
            "lifecycle_status": "TEXT NOT NULL DEFAULT 'Reported'",
            "reported_date": "TEXT",
        },
        "votes": {"direction": "INTEGER NOT NULL DEFAULT 1"},
    }
    for table, wanted in columns.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, declaration in wanted.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    conn.execute(
        """UPDATE issues SET subcategory=category,
             category_group=CASE
               WHEN category IN ('Pothole','Road Damage','Illegal Construction') THEN 'Roads and Infrastructure'
               WHEN category='Water Supply' THEN 'Water Supply'
               WHEN category IN ('Electrical Hazard','Streetlight') THEN 'Electricity'
               WHEN category IN ('Drainage/Sewage','Garbage & Waste') THEN 'Sanitation'
               WHEN category='Traffic Signal' THEN 'Transportation'
               ELSE 'Public Safety' END
           WHERE category_group IS NULL OR subcategory IS NULL"""
    )
    conn.execute("UPDATE issues SET reported_date=substr(created_at,1,10) WHERE reported_date IS NULL")
    conn.execute(
        """UPDATE issues SET lifecycle_status=CASE status
             WHEN 'Claimed' THEN 'Assigned'
             WHEN 'In Progress' THEN 'In Progress'
             WHEN 'Resolved' THEN 'Resolved'
             ELSE 'Reported' END
           WHERE lifecycle_status='Reported' AND status!='Reported'"""
    )
    conn.execute(
        """UPDATE users SET portal_role='citizen', politician_status='none'
           WHERE role!='citizen' AND portal_role='citizen'"""
    )


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------
def _seed(conn):
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def ts(hours_ago=0):
        return (now - timedelta(hours=hours_ago)).isoformat(timespec="seconds")

    def add_user(name, email, password, role, skill=None, loc=None, label=None):
        lat, lng = loc if loc else (None, None)
        cur = conn.execute(
            """INSERT INTO users (name, email, password_hash, role, skill, lat, lng, location_label, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (name, email, generate_password_hash(password), role, skill, lat, lng, label, ts(150)),
        )
        return cur.lastrowid

    add_user("Municipal Admin", "admin@urbanpulse.gov", "Admin@123", "admin")
    citizen = add_user("Asha Rao", "citizen@urbanpulse.gov", "Citizen@123", "citizen")
    ravi = add_user("Ravi Kumar", "worker@urbanpulse.gov", "Worker@123", "worker",
                    "Electrician", (12.9784, 77.6408), "Indiranagar")
    suresh = add_user("Suresh Gowda", "plumber@urbanpulse.gov", "Worker@123", "worker",
                      "Plumber", (12.9422, 77.5738), "Basavanagudi")
    manoj = add_user("Manoj Patil", "roadcrew@urbanpulse.gov", "Worker@123", "worker",
                     "Road Repair Technician", (12.9166, 77.6101), "BTM Layout")

    sponsor_rows = [
        ("SafeRoads Infra Pvt Ltd", "Pothole", 200, 10000),
        ("SafeRoads Infra Pvt Ltd", "Road Damage", 180, 6000),
        ("PowerSecure Utilities", "Electrical Hazard", 300, 6000),
        ("PowerSecure Utilities", "Streetlight", 120, 4000),
        ("AquaCare Foundation", "Water Supply", 150, 4500),
        ("AquaCare Foundation", "Drainage/Sewage", 130, 3000),
        ("CleanCity Trust", "Garbage & Waste", 100, 3000),
        ("Civic Impact Fund", "All", 75, 5000),
    ]
    sponsor_id = {}
    for name, category, reward, budget in sponsor_rows:
        cur = conn.execute(
            """INSERT INTO sponsors (name, category, reward_per_issue, budget_total, budget_remaining, created_at)
               VALUES (?,?,?,?,?,?)""",
            (name, category, reward, budget, budget, ts(190)),
        )
        sponsor_id[(name, category)] = cur.lastrowid

    # (title, description, category, lat, lng, address, status, upvotes)
    demo_issues = [
        ("Deep pothole near BTM bus stop",
         "A large pothole has formed near the BTM Layout bus stop, causing two-wheelers to swerve dangerously into traffic.",
         "Pothole", 12.9166, 77.6101, "BTM Layout 2nd Stage", "In Progress", 21),
        ("Overflowing garbage bin, 4th Cross",
         "The community bin has not been cleared in over a week and is overflowing onto the footpath.",
         "Garbage & Waste", 12.9279, 77.6271, "4th Cross, Koramangala", "Reported", 9),
        ("Streetlight outage on Park Road",
         "Three consecutive streetlights are dead, making the stretch unsafe for pedestrians after 8 PM.",
         "Streetlight", 12.9698, 77.5910, "Park Road, Shivajinagar", "Reported", 6),
        ("Open manhole cover near school gate",
         "An uncovered manhole has been sitting outside the primary school gate for two days — a serious risk for children.",
         "Drainage/Sewage", 12.9352, 77.6146, "Adugodi School Road", "Reported", 34),
        ("No water supply for 3 days",
         "Households along this stretch have had zero water supply for three days with no communication from the water board.",
         "Water Supply", 12.9500, 77.5850, "Basavanagudi Main Road", "In Progress", 18),
        ("Exposed live wire after storm",
         "Last night's storm brought down an electrical wire that is now exposed and dangling close to a pedestrian path.",
         "Electrical Hazard", 12.9611, 77.6387, "Indiranagar 100ft Road", "Claimed", 27),
        ("Faded pedestrian crossing markings",
         "The zebra crossing near the junction has faded completely and vehicles no longer slow down.",
         "Road Damage", 12.9750, 77.6050, "Cunningham Road Junction", "Resolved", 11),
        ("Traffic signal stuck on all-red",
         "The signal has been stuck on red for two days, causing a minor accident this morning.",
         "Traffic Signal", 12.9880, 77.5760, "Malleshwaram Circle", "In Progress", 29),
        ("Illegal construction blocking drain",
         "Ongoing construction has dumped debris into the storm water drain; the last rain already flooded the road.",
         "Illegal Construction", 12.9450, 77.6400, "HSR Layout Sector 3", "Reported", 15),
        ("Sewage overflow near market",
         "Sewage is overflowing from a blocked line outside the vegetable market — a health hazard for vendors.",
         "Drainage/Sewage", 12.9600, 77.6000, "KR Market", "Resolved", 8),
        ("Second pothole widening near bus stop",
         "Next to the earlier pothole, the surface is cracking further and widening into a second hazard.",
         "Pothole", 12.9160, 77.6108, "BTM Layout 2nd Stage", "Reported", 14),
        ("Garbage dumped illegally near bus stop",
         "Someone has started dumping household garbage on the same stretch as the pothole complaint.",
         "Garbage & Waste", 12.9171, 77.6098, "BTM Layout 2nd Stage", "Reported", 7),
    ]

    issue_id = {}
    for i, (title, desc, cat, lat, lng, addr, status, votes) in enumerate(demo_issues):
        created = ts(24 * (i + 1))
        lifecycle = {
            "Reported": "Reported", "Claimed": "Assigned",
            "In Progress": "In Progress", "Resolved": "Resolved",
        }[status]
        cur = conn.execute(
            """INSERT INTO issues (title, description, category, lat, lng, address, status, priority,
                                   upvotes, reporter_id, created_at, updated_at, category_group,
                                   subcategory, severity, lifecycle_status, reported_date)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (title, desc, cat, lat, lng, addr, status, classify_priority(title, desc, cat),
             votes, citizen, created, created,
             {
                 "Pothole": "Roads and Infrastructure", "Road Damage": "Roads and Infrastructure",
                 "Illegal Construction": "Roads and Infrastructure", "Water Supply": "Water Supply",
                 "Electrical Hazard": "Electricity", "Streetlight": "Electricity",
                 "Drainage/Sewage": "Sanitation", "Garbage & Waste": "Sanitation",
                 "Traffic Signal": "Transportation",
             }.get(cat, "Public Safety"),
             cat, classify_priority(title, desc, cat), lifecycle, created[:10]),
        )
        issue_id[title] = cur.lastrowid
        conn.execute("INSERT INTO status_log (issue_id, status, note, changed_at) VALUES (?,?,?,?)",
                     (cur.lastrowid, "Reported", "Issue reported by citizen.", created))
        if status == "In Progress":
            conn.execute("INSERT INTO status_log (issue_id, status, note, changed_at) VALUES (?,?,?,?)",
                         (cur.lastrowid, status, "Taken up by the ward office's own crew.", created))

    def reserve(issue, worker, sponsor, claimed_hours_ago):
        """Escrow a sponsor's reward on an issue exactly as the claim endpoint does."""
        amount = conn.execute("SELECT reward_per_issue FROM sponsors WHERE id=?", (sponsor,)).fetchone()[0]
        conn.execute("UPDATE sponsors SET budget_remaining = budget_remaining - ? WHERE id=?", (amount, sponsor))
        conn.execute("""UPDATE issues SET claimed_by=?, claimed_at=?, reward_amount=?, reward_sponsor_id=?
                        WHERE id=?""", (worker, ts(claimed_hours_ago), amount, sponsor, issue))
        conn.execute("INSERT INTO status_log (issue_id, status, note, changed_at) VALUES (?,?,?,?)",
                     (issue, "Claimed", "Claimed by a skilled worker — reward reserved.", ts(claimed_hours_ago)))
        return amount

    def complete(issue, worker, sponsor, note, claimed_hours_ago, resolved_hours_ago):
        amount = reserve(issue, worker, sponsor, claimed_hours_ago)
        conn.execute("""UPDATE issues SET resolved_by=?, resolution_note=?, reward_paid=1,
                        verification_status='auto_approved', verification_score=100, updated_at=? WHERE id=?""",
                     (worker, note, ts(resolved_hours_ago), issue))
        conn.execute("UPDATE users SET wallet_balance = wallet_balance + ? WHERE id=?", (amount, worker))
        conn.execute("""INSERT INTO reward_ledger (issue_id, worker_id, sponsor_id, amount, created_at)
                        VALUES (?,?,?,?,?)""", (issue, worker, sponsor, amount, ts(resolved_hours_ago)))
        conn.execute("INSERT INTO status_log (issue_id, status, note, changed_at) VALUES (?,?,?,?)",
                     (issue, "Resolved", f"{note} Reward of ₹{amount:.0f} paid.", ts(resolved_hours_ago)))

    # One job in progress by a worker (reward already reserved) and two completed, paid jobs.
    reserve(issue_id["Exposed live wire after storm"], ravi,
            sponsor_id[("PowerSecure Utilities", "Electrical Hazard")], 20)
    complete(issue_id["Faded pedestrian crossing markings"], manoj,
             sponsor_id[("SafeRoads Infra Pvt Ltd", "Road Damage")],
             "Repainted the crossing with reflective paint.", 60, 58)
    complete(issue_id["Sewage overflow near market"], suresh,
             sponsor_id[("AquaCare Foundation", "Drainage/Sewage")],
             "Cleared the blockage and resealed the line.", 30, 28)
