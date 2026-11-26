# CivicConnect Implementation Tracker

**Project status:** Confirmed development scope implemented; production ML evaluation remains pending  
**Last reviewed:** 2026-11-26  
**Application code changes:** Implemented for the confirmed scope; model evaluation on real data remains pending

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
- [x] Verify politician accounts through both admin approval and official-email
  verification.
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
- [x] Add comments and one up/down vote per user, with self-vote and resolved
  issue rules defined.
- [x] Add follows for politicians and issues.
- [x] Build a ranked homepage/feed for popular and relevant posts, without
  letting popularity obscure urgent/high-severity complaints.

### Phase 4 — ML intelligence
- [x] Add TF-IDF + linear SVM category, subcategory, and severity suggestions;
  expose the provisional margin and allow citizens to correct/confirm.
- [x] Require reporter review before submitting a report using the new taxonomy;
  do not apply an arbitrary threshold to an uncalibrated margin.
- [x] Add duplicate-candidate detection using text similarity plus location;
  present candidates for confirmation rather than silently merging complaints.
- [ ] Measure model performance on a real, held-out labeled dataset. The
  approved 54-example starter corpus is development-only; production use must
  wait for local labeled data and evaluation.
- [ ] Add a production training/evaluation pipeline once real labeled data is
  available.

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

- **ML corpus:** `data/civic_training.csv` contains 54 provisional development
  examples across six categories and 18 subcategories. No local production
  dataset or held-out accuracy evaluation exists yet. The shown SVM margin is
  explicitly not a calibrated probability; reporters confirm the suggested
  category/severity before submitting.
- **Duplicate detection:** TF-IDF similarity is combined with a 500 m proximity
  check. Matches are review candidates, never auto-merged.
- **Politician verification:** SMTP and `URBANPULSE_PUBLIC_URL` must be
  configured. Registration requires an allowed government email domain, a
  verification link, and admin approval.
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
- Require admin approval and official-email verification before activating a
  politician account.
- Use a provisional starter corpus for development only; production readiness
  depends on a real labeled dataset and measured evaluation results.

## Change log

| Date | Change | Validation |
|---|---|---|
| 2026-11-26 | Reviewed README, application routes, schema, intelligence module, templates, frontend scripts, and tests; added this initial inventory and roadmap. No application behavior changed. | `python -m unittest discover -s tests -v` — 22 tests passed. |
| 2026-11-26 | Recorded confirmed scope: preserve existing worker/admin/reward features; politician activation requires admin approval and official-email verification; development-only starter ML corpus. | Planning decision; no application behavior changed. |
| 2026-11-26 | Added additive schema upgrades, politician verification/review, the seven-stage lifecycle, image/video media, comments, directional votes, issue/politician follows, popularity ranking, Civic XP/profiles, and accountability metrics. | 32 tests pass; existing worker/reward regression tests retained. |
| 2026-11-26 | Added provisional TF-IDF + linear SVM classification, reporter confirmation, location-aware duplicate candidates, ML corpus, and updated README/setup guidance. | 54 development examples; sample drainage prediction returns Sanitation / Drainage / Medium; no production/held-out accuracy claim. |
