# CivicConnect Implementation Tracker

**Project status:** CivicConnect implementation complete; real-data ML transfer baseline integrated  
**Last reviewed:** 2026-11-28  
**Application code changes:** Implemented for confirmed scope; local production ML validation remains pending

This tracker records the baseline UrbanPulse behavior, requested CivicConnect
features, the confirmed delivery sequence, and changes made during implementation.

## Existing project at initial review

The project is a Flask + SQLite application with server-rendered HTML, plain
JavaScript/CSS, Leaflet maps, and Server-Sent Events. It currently focuses on
civic reports, skilled-worker jobs, and sponsor-funded rewards.

### Features already present

- **Accounts and roles:** Login/registration for citizens, skilled workers, and
  administrators. Admin registration is gated by an invite code.
- **Issue reporting:** Citizens create text reports with a title, description,
  one of ten categories, map/GPS location, optional address, and optional photo.
  Report timestamps are recorded automatically.
- **Issue map and feed:** Leaflet live map, place/report search, category/status
  filters, personal report filter, live activity feed, and SSE refreshes.
- **Community signal:** One toggleable upvote per eligible user; users cannot
  upvote their own or resolved reports. Reports are ranked by an impact score
  combining priority, votes, and age.
- **Rule-based intelligence:** Category/text keyword priority, impact ranking,
  500 m open-issue hotspot clustering, and heuristic worker completion-photo
  checks. These are not an ML text classifier or duplicate detector.
- **Worker workflow:** Skill-matched, nearby job board; claim/release; maximum
  three active claims; completion note/photo; sponsor reward escrow, ledger,
  wallet, and earnings history.
- **Admin tools:** Issue oversight/status control, category/status/trend
  analytics, hotspots, photo-review queue, sponsor budgets, worker leaderboard,
  and CSV export.
- **Safeguards:** CSRF checks, role-protected routes, password hashing,
  image validation/re-encoding, payout controls, and CSV formula neutralization.
- **Baseline tests:** 22 existing unit/workflow tests passed before implementation.

## Requested features and current gaps

| Requested capability | Current state |
|---|---|
| Citizen and politician accounts | Citizen exists; politician role/profile/authorization does not. Current worker/admin roles also exist. |
| Text, images, and videos on posts | Text and optional image exist; videos and multiple attachments do not. |
| Upvote, downvote, and comments | Upvote exists; downvote and comments do not. |
| Popular posts at the top | Issues are impact-ranked; a social/popularity feed is not present. |
| Follow politicians and civic issues | Not present. |
| Complaint lifecycle and resolution tracking | Timeline exists, but statuses are `Reported`, `Claimed`, `In Progress`, and `Resolved`; the requested seven-stage lifecycle is not implemented. |
| Civic XP and contribution statistics | Not present. Worker reward earnings are monetary, not Civic XP. |
| Profile graphs and politician accountability | Admin charts exist; citizen/politician profiles and their contribution/performance graphs do not. |
| Issue category, description, media, location, politician, severity, report date | Category, description, location, optional image, priority, and automatic report timestamp exist. Politician assignment and explicit user-verifiable severity do not. |
| TF-IDF + SVM classification | Not present. Current priority is deterministic keyword/category rules. No labeled ML training corpus is present. |
| Duplicate complaint detection | Not present. Hotspot clustering groups nearby reports but does not decide whether reports are duplicates. |

## Proposed plan of action

### Phase 0 — Confirm scope and data
- [x] Preserve the existing worker, sponsor-reward, and admin workflows
  alongside the new citizen/politician experience.
- [x] Require admin approval for politician accounts; email verification is not
  required (confirmed 2026-11-28).
- [x] Use a clearly marked, development-only starter corpus; obtain real labeled
  data and evaluate the model before production use.

- [x] Extend the SQLite schema for politicians, issue assignments, follows,
  comments, directional votes, media, XP/events, and the full status history.
- [x] Define role permissions and permitted lifecycle transitions.
- [x] Add a safe, repeatable schema-upgrade path that preserves existing demo
  and user data; update seed data and README setup instructions.

### Phase 2 — Issue reporting and lifecycle
- [x] Expand issue creation to capture category/subcategory, description,
  severity, location, concerned politician, reporting date, and media.
- [x] Implement and audit transitions:
  `Reported → Under Review → Acknowledged → Assigned → In Progress → Resolved → Verified`.
- [x] Add image/video upload validation, storage limits, and authorization.
- [x] Preserve the current photo/reward workflow if Phase 0 confirms it remains
  in scope.

### Phase 3 — Civic social features
- [x] Add comments and one up/down vote per user; resolved issues are closed to
  voting, while reporters may vote on their own open reports.
- [x] Add follows for politicians and issues.
- [x] Build a ranked homepage/feed for popular and relevant posts, without
  letting popularity obscure urgent/high-severity complaints.

### Phase 4 — ML intelligence
- [x] Add TF-IDF + linear SVM category and subcategory suggestions from real NYC
  311 records; severity remains deterministic because the source has no severity
  labels.
- [x] Keep category and severity suggestions editable, without requiring a
  separate confirmation checkbox before report submission.
- [x] Add duplicate-candidate detection using text similarity plus location;
  present candidates for confirmation rather than silently merging complaints.
- [x] Measure category and end-to-end subcategory performance on a real,
  grouped 75/25 held-out dataset.
- [x] Add a reproducible download, curation, training, and evaluation pipeline.
- [ ] Validate with representative local citizen narratives before production
  use; NYC structured labels are only a transfer baseline.

### Phase 5 — Profiles, XP, and accountability
- [x] Define transparent XP rules for verified citizen contributions and
  politician issue handling/resolution.
- [x] Add profile contribution summaries and monthly/category graphs.
- [x] Add politician performance/accountability views based on auditable issue
  history, including resolution and verification outcomes.

### Phase 6 — Verification and documentation
- [x] Add focused tests for role permissions, lifecycle transitions, media,
  voting/comments/follows, XP, classification, and duplicate candidates.
- [x] Run the existing suite plus targeted checks; verify existing worker/admin
  behavior where retained. Final suite: 32 tests pass; Python/JavaScript syntax
  checks pass.
- [x] Update README feature descriptions, roles, setup, ML data/training steps,
  known limitations, and test instructions.
- [x] Update this tracker with completed work, validation results, and remaining
  limitations.

## Implementation notes and remaining limitations

- **ML corpus:** `data/civic_training_real.csv` contains 389 unique structured
  records from NYC Open Data dataset `erm2-nwe9`, mapped to six CivicConnect
  categories. `scripts/train_civic_model.py` downloads, evaluates, and exports
  the corpus; metrics and retrieval timestamp are recorded in
  `data/civic_model_metrics.json`. Grouped 75/25 holdout results: category
  accuracy 0.9796 / macro-F1 0.9743; end-to-end subcategory accuracy 0.9388 /
  macro-F1 0.7697 (98 held-out records). This does not establish accuracy on
  local narrative reports. Severity is rule-based because the source has no
  severity labels; model margin is not calibrated confidence.
- **Duplicate detection:** TF-IDF similarity is combined with a 500 m proximity
  check. Matches are review candidates, never auto-merged.
- **Politician approval:** Registration accepts any valid email. Politicians
  cannot sign in until an administrator approves their account. Legacy email
  verification columns remain in SQLite for compatibility but are no longer
  used by application logic.
- **Media:** Up to five attachments; videos are MP4/WebM and max 20 MB each;
  requests are capped at 25 MB.
- **XP:** Citizen reports earn XP only after verification; one comment XP award
  is available per user/issue. Politicians earn XP for acknowledgment,
  resolution, and citizen verification. These rules are recorded in
  `app.py`'s `award_xp` calls.

## Confirmed decisions

The initial scope decisions have been confirmed:

- Keep skilled workers, sponsors, rewards, and administrators as additional
  roles.
- Require admin approval before activating a politician account; email
  verification is not required (updated 2026-11-28).
- Use NYC 311 as an initial real-data transfer baseline; production readiness
  depends on representative local narrative data and measured evaluation.

## Change log

| Date | Change | Validation |
|---|---|---|
| 2026-11-26 | Reviewed README, application routes, schema, intelligence module, templates, frontend scripts, and tests; added this initial inventory and roadmap. No application behavior changed. | `python -m unittest discover -s tests -v` — 22 tests passed. |
| 2026-11-26 | Recorded confirmed scope: preserve existing worker/admin/reward features; politician activation requires admin approval and official-email verification; development-only starter ML corpus. | Planning decision; no application behavior changed. |
| 2026-11-26 | Added additive schema upgrades, politician verification/review, the seven-stage lifecycle, image/video media, comments, directional votes, issue/politician follows, popularity ranking, Civic XP/profiles, and accountability metrics. | 32 tests pass; existing worker/reward regression tests retained. |
| 2026-11-26 | Added provisional TF-IDF + linear SVM classification, reporter confirmation, location-aware duplicate candidates, ML corpus, and updated README/setup guidance. | 54 development examples; sample drainage prediction returns Sanitation / Drainage / Medium; no production/held-out accuracy claim. |
| 2026-11-28 | Removed politician email/domain verification; politicians can register with any valid email but require admin approval to sign in. Removed mandatory category/severity confirmation checkbox/server rejection; category and severity remain editable. | Regression tests cover admin approval gating and successful submission without a confirmation field. |
| 2026-11-28 | Replaced the provisional runtime classifier corpus with a real NYC 311 training snapshot; added reproducible download/curation, grouped holdout evaluation, category/subcategory SVMs, shared drainage terminology normalization, and rule-based severity (no real severity labels available). | 389 unique records; 98-record grouped holdout: category accuracy 0.9796 / macro-F1 0.9743; end-to-end subcategory accuracy 0.9388 / macro-F1 0.7697. All 32 tests pass; Python and JavaScript syntax checks pass; all training labels match the app taxonomy. |
| 2026-11-28 | Fixed vote totals so upvote/downvote switching and removal adjust the stored count instead of replacing legacy totals with rows from the new votes table. | Regression test covers preserving seeded upvotes through upvote, direction switch, and removal; browser check confirms counts update correctly. |
| 2026-11-28 | Added an inline reason when vote controls are disabled. At that time, the default Citizen owned the demo-seeded reports and self-voting was blocked; resolved issues were also closed to voting. | Verified the default seed ownership and disabled-state logic; active controls worked for other users' unresolved reports. |
| 2026-11-28 | Enabled upvotes and downvotes on a reporter's own open issues; only resolved/verified issues remain closed to voting. | Citizen API regression test verifies POST upvote, POST downvote switch, and POST vote removal. |
