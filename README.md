# CivicConnect — AI-Assisted Civic Social Platform

Citizens report and discuss local problems; verified politicians track and update
their resolution; and everyone can follow issues, vote, comment, and see public
contribution/accountability profiles. The existing skilled-worker repair,
sponsor-funded reward, and municipal admin workflows remain available.

| Role | Can do |
|---|---|
| **Citizen** | Report issues with text, images/video, category, severity, date and location; vote, comment, follow, verify resolution, earn Civic XP |
| **Politician** | Register and await admin approval, follow assigned issues through the lifecycle, earn Civic XP |
| **Worker** | See funded jobs matching their skill and service area; claim, repair, submit proof photo, and receive sponsor-funded rewards |
| **Admin** | Review politician accounts, manage civic issue stages, view analytics/hotspots, manage sponsor budgets, review flagged payouts |

**Stack:** Flask + SQLite (no ORM) · Pillow · scikit-learn (TF-IDF + linear SVM) ·
Server-Sent Events · Leaflet map · plain HTML/CSS/JS.

## Run

```bash
pip install -r requirements.txt
python app.py                      # http://127.0.0.1:5000  (DB is created + seeded automatically)
python -m unittest discover -s tests -v    # tests for civic and reward workflows
```

| Role | Email | Password |
|---|---|---|
| Admin | admin@urbanpulse.gov | Admin@123 |
| Citizen | citizen@urbanpulse.gov | Citizen@123 |
| Electrician | worker@urbanpulse.gov | Worker@123 |
| Plumber | plumber@urbanpulse.gov | Worker@123 |
| Road repair | roadcrew@urbanpulse.gov | Worker@123 |

Admin registration needs an invite code (`URBANPULSE_ADMIN_CODE`, default `URBANPULSE-ADMIN-2026`).
Delete `urbanpulse.db` to reset the demo data. GPS needs `localhost` or HTTPS
(`URBANPULSE_HTTPS=1 python app.py` + `pip install cryptography` for testing from a phone).
The map, place search and fonts load from public services, so those need internet.

Politician registration requires administrator approval before the account can
sign in. No email verification or SMTP configuration is required.
The SQLite schema upgrades additively when the app starts and preserve existing
accounts and issue/reward data.

---

## Design decisions — "why is it done this way?"

### 1. Money: escrow instead of "pay at the end"
When a worker **claims** a job, the sponsor's reward is *reserved on that job*. It is paid into the
worker's wallet when the proof photo is verified, and refunded to the sponsor if the worker releases the job.
* The reward shown on the board is a promise, not an estimate: the budget can't run out between claim and payout.
* The budget check is inside the SQL `UPDATE ... WHERE budget_remaining >= reward`, and the claim lock is
  `WHERE status='Reported' AND claimed_by IS NULL`, both in one transaction — two workers can't take the same job or the same last rupees.
* `reward_paid = 0` in the payout query makes paying one job twice impossible. Only an *active* claim can be released or resolved.
* Jobs whose category has no sponsor funding aren't shown as claimable (nobody should work for an unfunded promise); the board tells the worker how many are waiting.

### 2. Who may claim what
* A worker only sees/claims categories matching their profession, within their own service radius.
* "Illegal Construction" is an enforcement matter, so no worker profession can claim it.
* A worker can hold at most 3 jobs — a claim is a public lock, and without a cap one person could lock the board.

### 3. Intelligence layer (`intelligence.py`)
* **Priority** = the higher of (category default, danger keywords). Category matters: an "Electrical Hazard" is
  dangerous even if the text never says "live wire". Keywords match whole words (`fire` ≠ `fireplace`).
* **Impact score (0–100)** = severity (10–50) + votes (max 30) + waiting time (max 20).
  Votes are capped because sign-up is open; waiting time *raises* urgency so old, neglected issues rise
  instead of being buried by new reports.
* **Hotspots** = single-linkage clustering: open issues within 500 m of each other (directly or through a chain),
  minimum 3 (two may just be duplicates). A fixed grid would split a real cluster that crosses a grid line.
* **Photo verification** is *not* computer vision and says so. It catches the cheapest frauds: junk photos
  (dark/blank/blurry) and re-submitting the citizen's original photo (perceptual hash). Stats are computed on a 256 px
  copy so thresholds don't depend on camera resolution. A flagged photo is never auto-rejected — a human
  admin approves (pay) or rejects (worker retakes the photo, keeps the lock and the escrow).
* **Text suggestions** use TF-IDF + linear SVM classifiers trained on real NYC 311 complaint labels for the
  category and subcategory. Inputs are the title and description; a small shared normalization maps drainage
  phrases to matching NYC sewer terminology. Severity remains deterministic and rule-based because this source
  has no severity labels. Citizens can review and edit suggestions. The displayed category margin is not a
  calibrated probability.
* **Duplicate candidates** combine TF-IDF text similarity with a 500 m location check. They are suggestions only:
  reports are never automatically merged or hidden.

The checked-in `data/civic_training_real.csv` snapshot contains 389 unique structured records from NYC Open Data's
311 Service Requests dataset (`erm2-nwe9`), spanning six mapped civic categories. The training/evaluation
pipeline is reproducible with:

```bash
python scripts/train_civic_model.py
```

It downloads the latest selected records, evaluates a grouped 75/25 holdout, and writes the training CSV plus
detailed metrics to `data/civic_model_metrics.json`. The current snapshot was retrieved 2026-11-28; the held-out
results are **97.96% category accuracy / 0.9743 macro-F1** and **93.88% end-to-end subcategory accuracy /
0.7697 macro-F1** on 98 held-out records. The lower subcategory macro-F1 reflects rare classes. The app loads
the saved snapshot at startup, so retraining requires restarting the app.

The NYC source provides structured problem labels/descriptors, not citizen-written narratives, and its terms may
not match the deployment region. NYC's dataset metadata has no populated license identifier; check its current
terms before redistributing the snapshot outside this project. These results are a held-out transfer baseline,
not local or production validation.

### 4. Civic issue accountability
Issues use a separate civic lifecycle—`Reported → Under Review → Acknowledged → Assigned → In Progress → Resolved → Verified`—
alongside the original worker/reward status fields. Assigned politicians or admins advance review stages; only the
reporting citizen verifies a resolved issue. Existing worker claims and reward safeguards continue to use their
original status and escrow rules.

The report form uses six broad categories: Roads and Infrastructure, Water
Supply, Electricity, Sanitation, Public Safety, and Transportation, with
subcategory choices such as potholes, drainage, streetlights, and traffic
signals.

Citizen XP is awarded for a verified report, and for contributing comments (once per issue per user). Politicians
earn XP for acknowledging/resolving assigned work and when the reporting citizen verifies the issue. Profile charts
and accountability counts are based on recorded reports and lifecycle events.

### 5. Admin powers are limited by the same rules
An admin can't mark a worker-held job "Resolved" (that would skip the proof photo and the payout logic) — the only
override is **take back → Reported**, which refunds the escrow. A civic issue's `Resolved → Verified` transition is
reserved for its reporting user; the legacy worker reward status remains final after resolution.

### 6. Security
* CSRF token on every POST (header for API calls, hidden field for forms) + `SameSite=Lax` cookies.
* Uploads are trusted by *content*: decoded with Pillow, auto-rotated, metadata (EXIF/GPS) dropped, re-encoded, saved under a random name.
* Passwords hashed (Werkzeug scrypt, min 8 chars); fresh session on every login; random secret key unless `URBANPULSE_SECRET` is set.
* CSV export neutralises spreadsheet-formula injection (`=HYPERLINK(...)` typed into a title).
* Role checks are enforced on the server for every endpoint, not only hidden in the UI.

### 7. Engineering choices
* **SQLite, no ORM** — one file, zero setup; `CHECK` constraints and foreign keys let the DB itself refuse bad states.
* **SSE, not WebSockets** — updates only flow server → browser. Events carry a tiny public summary; each page re-fetches
  what it shows (debounced), so one user's private data is never broadcast to everyone.
* **One SQL query with JOINs** returns an issue with worker names and vote flag (previously 3–4 queries per issue); worker job search uses a bounding-box filter in SQL before exact distance.
* **Plain CSS bars** instead of a chart library for three simple distributions.
* **Browser downsizes photos** before upload (phone photos are 3–10 MB), so the size limit is rarely hit.

## What was removed, and why
| Removed | Reason |
|---|---|
| Role selector on the login form | The account already has a role; the tab could only be wrong |
| "Ward" field | Collected but never used — admins aren't ward-scoped |
| `audit_log` table | Written but there was no way to read it; failed admin sign-ups are now logged to the server log |
| Fake landing stats ("<1s latency", "24/7 uptime") | Unmeasured claims; replaced with real live counts |
| IP-based "approximate location" | Can be a different city; useless for pinning a pothole or choosing a work area |
| In-browser camera widget (getUserMedia + canvas) | The phone's own photo picker already offers the camera (`capture` attribute on worker proof) |
| "Enter coordinates manually" | Nobody knows their coordinates; GPS or a map tap covers it |
| DB migration code | Columns are in the schema; there are no legacy databases to migrate |
| Duplicate seed priority function, duplicated sponsor-lookup SQL, duplicate JS helpers | One implementation each |
| Optional-Pillow fallback | The photo-verification feature depends on it, so it is a real requirement |
| Chart.js + fallback code | Replaced by CSS bars |
| Hard-coded secret key, `0.0.0.0` default bind | Random key; binds to localhost unless `URBANPULSE_HOST` is set |

## Honest limitations
* No rate limiting on login / invite code (the code is logged on failure).
* The real-data model is an NYC-to-local taxonomy transfer baseline, not validated on local citizen narratives.
  Add representative locally labeled complaints and reevaluate before production use. Severity is rule-based;
  the model margin is not calibrated confidence.
* Politician accounts require administrator approval before sign-in.
* The photo check can't tell *what* is in a photo, only that it isn't junk or a copy.
  Thresholds (score ≥ 70, brightness, detail) are reasonable defaults, not tuned on real data; a dimly lit night repair may be flagged — costing the admin one click, not the worker their pay.
* Wallet is a ledger balance; there is no withdrawal/bank integration.
* SSE subscribers live in process memory, so run a single server process (use Redis pub/sub to scale out).

## Structure
```
app.py             routes, auth, CSRF, REST API, SSE, civic workflow, escrow/payout logic
database.py        schema, additive upgrades, demo seed
intelligence.py    TF-IDF/SVM suggestions, duplicate candidates, priority, hotspots, photo checks
civic_text.py      shared model text normalization
scripts/           real-data training and evaluation pipeline
data/              NYC 311 training snapshot and held-out metrics
tests/             civic/social workflow and business-rule tests
templates/ static/  landing, issue console, worker/admin consoles, politician directory, profiles
```
