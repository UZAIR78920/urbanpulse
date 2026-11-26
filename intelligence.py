"""
UrbanPulse — intelligence.py
The rule-based "civic intelligence" layer. Everything here is deterministic
and explainable: for any output you can point at the exact rule that
produced it, which is the point for a public-service system where a citizen
or an auditor may ask "why was this ranked first?" or "why was this payout
held back?".

  1. classify_priority()        severity from report text + category
  2. impact_score()             worklist ranking (0-100): severity + votes + waiting time
  3. detect_hotspots()          distance-based clustering of open issues
  4. analyze_photo() / verify_completion_photo()
                                fraud checks on a worker's proof-of-work photo
"""

import math
import csv
import re
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageFilter
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.svm import LinearSVC


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance in km. Used for worker service areas and hotspots."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# 1. Priority classification
# ---------------------------------------------------------------------------
# Two signals are combined and the HIGHER one wins:
#   a) the category the citizen picked   - an "Electrical Hazard" is dangerous
#      even if the description never says "live wire";
#   b) danger keywords in the text       - a "Garbage" report that mentions a
#      child or a fire is escalated beyond the category's default.
# Taking the max (not an average) is deliberate: for safety, one strong danger
# signal should never be diluted by a mild one.
RANK = {"Low": 0, "Medium": 1, "High": 2}

CATEGORY_BASE_PRIORITY = {
    "Electrical Hazard": "High",
    "Drainage/Sewage": "Medium",
    "Water Supply": "Medium",
    "Pothole": "Medium",
    "Road Damage": "Medium",
    "Traffic Signal": "Medium",
    "Streetlight": "Low",
    "Garbage & Waste": "Low",
    "Illegal Construction": "Low",
    "Other": "Low",
}

HIGH_RISK_TERMS = [
    "fire", "collapse", "collapsed", "gas leak", "electrocution", "live wire",
    "exposed wire", "accident", "flood", "flooding", "explosion", "injury",
    "injured", "death", "drowning", "manhole", "short circuit",
    "child", "children", "school",
]
MEDIUM_RISK_TERMS = [
    "leak", "leaking", "overflow", "overflowing", "broken", "damaged", "blocked",
    "no water", "power cut", "not working", "stuck", "debris", "pothole",
]

# \b word boundaries stop "fire" matching "fireplace" or "school" matching "preschooler".
_HIGH_RE = re.compile(r"\b(?:" + "|".join(map(re.escape, HIGH_RISK_TERMS)) + r")\b")
_MEDIUM_RE = re.compile(r"\b(?:" + "|".join(map(re.escape, MEDIUM_RISK_TERMS)) + r")\b")


def classify_priority(title: str, description: str, category: str = "Other") -> str:
    text = f"{title} {description}".lower()
    keyword_level = "High" if _HIGH_RE.search(text) else "Medium" if _MEDIUM_RE.search(text) else "Low"
    category_level = CATEGORY_BASE_PRIORITY.get(category, "Low")
    return max(keyword_level, category_level, key=RANK.get)


# ---------------------------------------------------------------------------
# 1b. Provisional text classification (TF-IDF + linear SVM)
# ---------------------------------------------------------------------------
TRAINING_DATA_PATH = Path(__file__).resolve().parent / "data" / "civic_training.csv"


def _train_text_models():
    rows = []
    with TRAINING_DATA_PATH.open(encoding="utf-8", newline="") as source:
        rows.extend(csv.DictReader(source))
    if len(rows) < 12:
        raise RuntimeError(f"ML training corpus is too small: {TRAINING_DATA_PATH}")

    texts = [f"{row['title']} {row['description']}" for row in rows]
    vectorizer = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)
    matrix = vectorizer.fit_transform(texts)

    category_model = LinearSVC(class_weight="balanced", random_state=42).fit(
        matrix, [row["category"] for row in rows]
    )
    severity_model = LinearSVC(class_weight="balanced", random_state=42).fit(
        matrix, [row["severity"] for row in rows]
    )
    subcategory_models = {}
    for category in sorted({row["category"] for row in rows}):
        indexes = [i for i, row in enumerate(rows) if row["category"] == category]
        subcategory_models[category] = LinearSVC(class_weight="balanced", random_state=42).fit(
            matrix[indexes], [rows[i]["subcategory"] for i in indexes]
        )
    return vectorizer, category_model, subcategory_models, severity_model


_TEXT_VECTORIZER, _CATEGORY_MODEL, _SUBCATEGORY_MODELS, _SEVERITY_MODEL = _train_text_models()


def _margin_confidence(model, vector):
    """A normalized decision-margin indicator, not a calibrated probability."""
    margins = model.decision_function(vector)
    if not hasattr(margins, "tolist"):
        return 1.0
    values = margins.tolist()
    if isinstance(values, float):
        return 1.0
    if not isinstance(values, list):
        return 1.0
    if len(values) == 1 and isinstance(values[0], float):
        return round(1 / (1 + math.exp(-max(-20, min(20, abs(values[0]))))), 3)
    exp_values = [math.exp(max(-20, min(20, value))) for value in values[0]]
    total = sum(exp_values) or 1
    return round(max(exp_values) / total, 3)


def classify_issue(title, description):
    """Return provisional TF-IDF/SVM labels and an uncalibrated margin indicator."""
    vector = _TEXT_VECTORIZER.transform([f"{title} {description}"])
    category = _CATEGORY_MODEL.predict(vector)[0]
    subcategory = _SUBCATEGORY_MODELS[category].predict(vector)[0]
    severity = _SEVERITY_MODEL.predict(vector)[0]
    return {
        "category_group": str(category),
        "subcategory": str(subcategory),
        "severity": str(severity),
        "confidence": _margin_confidence(_CATEGORY_MODEL, vector),
        "model_status": "provisional",
    }


def find_duplicate_candidates(title, description, lat, lng, issues, radius_km=0.5, threshold=0.58):
    """Return nearby text-similar open issues as candidates; never merge automatically."""
    candidates = []
    text = f"{title} {description}".strip()
    for issue in issues:
        if issue["status"] == "Resolved":
            continue
        distance = haversine_km(lat, lng, issue["lat"], issue["lng"])
        if distance <= radius_km:
            candidates.append((issue, distance))
    if not candidates or not text:
        return []
    documents = [text] + [
        f"{issue['title']} {issue['description']}".strip() for issue, _distance in candidates
    ]
    matrix = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True).fit_transform(documents)
    similarities = cosine_similarity(matrix[0:1], matrix[1:]).ravel()
    matches = [
        {
            "id": issue["id"],
            "title": issue["title"],
            "category": issue["category"],
            "status": issue["status"],
            "distance_km": round(distance, 3),
            "similarity": round(float(similarity), 3),
        }
        for (issue, distance), similarity in zip(candidates, similarities)
        if similarity >= threshold
    ]
    return sorted(matches, key=lambda match: (-match["similarity"], match["distance_km"]))[:5]


# ---------------------------------------------------------------------------
# 2. Impact score (ward office worklist ranking)
# ---------------------------------------------------------------------------
# score (0-100) = severity (10-50) + community votes (max 30) + waiting time (max 20)
#
#  * severity      - what could go wrong if nothing is done.
#  * votes         - citizens independently confirming the problem. Capped,
#                    because registration is open and votes could be farmed.
#  * waiting time  - an unresolved issue gets MORE urgent the longer it waits
#                    (like an SLA clock), so old neglected issues rise up the
#                    list instead of being buried under newer reports.
SEVERITY_POINTS = {"High": 50, "Medium": 25, "Low": 10}
POINTS_PER_VOTE, MAX_VOTE_POINTS = 3, 30
POINTS_PER_DAY_OPEN, MAX_WAITING_POINTS = 2, 20


def impact_score(priority, upvotes, created_at_iso, status):
    if status == "Resolved":
        return -1.0  # finished work sinks to the bottom of any ranking

    votes = min(upvotes * POINTS_PER_VOTE, MAX_VOTE_POINTS)
    try:
        created = datetime.fromisoformat(created_at_iso)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        days_open = max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 86400)
    except (ValueError, TypeError):
        days_open = 0.0
    waiting = min(days_open * POINTS_PER_DAY_OPEN, MAX_WAITING_POINTS)

    return round(SEVERITY_POINTS.get(priority, 10) + votes + waiting, 1)


# ---------------------------------------------------------------------------
# 3. Hotspot detection
# ---------------------------------------------------------------------------
# Single-linkage clustering: two open issues belong to the same hotspot if they
# are within HOTSPOT_RADIUS_KM of each other, directly or through a chain of
# neighbours. Unlike a fixed lat/lng grid, this never splits a real cluster
# just because it straddles an arbitrary grid line.
# MIN_HOTSPOT_SIZE is 3: two nearby reports may simply be duplicates; three is
# a pattern worth sending a crew to look at.
HOTSPOT_RADIUS_KM = 0.5
MIN_HOTSPOT_SIZE = 3


def detect_hotspots(issues, radius_km=HOTSPOT_RADIUS_KM, min_size=MIN_HOTSPOT_SIZE):
    """`issues`: dicts with id, lat, lng, status, category, address."""
    open_issues = [i for i in issues if i["status"] != "Resolved"]
    parent = list(range(len(open_issues)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in range(len(open_issues)):
        for b in range(a + 1, len(open_issues)):
            ia, ib = open_issues[a], open_issues[b]
            if haversine_km(ia["lat"], ia["lng"], ib["lat"], ib["lng"]) <= radius_km:
                parent[find(a)] = find(b)

    groups = {}
    for idx, issue in enumerate(open_issues):
        groups.setdefault(find(idx), []).append(issue)

    clusters = []
    for members in groups.values():
        if len(members) < min_size:
            continue
        categories = {}
        for m in members:
            categories[m["category"]] = categories.get(m["category"], 0) + 1
        addresses = [m["address"] for m in members if m.get("address")]
        clusters.append({
            "count": len(members),
            "lat": round(sum(m["lat"] for m in members) / len(members), 5),
            "lng": round(sum(m["lng"] for m in members) / len(members), 5),
            "area": max(set(addresses), key=addresses.count) if addresses else "Unnamed area",
            "categories": categories,
            "dominant_category": max(categories, key=categories.get),
            "issue_ids": [m["id"] for m in members],
        })
    clusters.sort(key=lambda c: c["count"], reverse=True)
    return clusters


# ---------------------------------------------------------------------------
# 4. Completion-photo verification
# ---------------------------------------------------------------------------
# What this is NOT: a computer-vision model. It cannot tell a fixed pothole
# from an unfixed one. What it reliably catches are the two cheapest ways to
# cheat a pay-per-photo system:
#   1. a junk photo (black, blank, or too blurry to show anything), and
#   2. re-submitting the citizen's ORIGINAL photo as "proof" of the fix.
# A flagged photo is never auto-rejected; it goes to a human admin. The
# system is deliberately cautious about withholding a worker's pay by itself.
APPROVE_THRESHOLD = 70
DARK_LIMIT, BRIGHT_LIMIT = 20, 245      # mean grey level (0-255)
MIN_EDGE_DETAIL = 8                      # std-dev of edge response
SAME_PHOTO_HASH_DISTANCE = 4             # of 64 bits
PENALTY_BAD_EXPOSURE, PENALTY_NO_DETAIL, PENALTY_REUSED_PHOTO = 40, 30, 50
ANALYSIS_SIZE = 256                      # px; makes the stats independent of photo resolution


def _pixels(img):
    # Pillow 12.1+ renamed getdata(); support both so requirements can stay Pillow>=10
    return list(img.get_flattened_data() if hasattr(img, "get_flattened_data") else img.getdata())


def analyze_photo(path):
    """Returns brightness, edge_energy and a 64-bit average-hash, or None if unreadable."""
    try:
        gray = Image.open(path).convert("L")
        gray.thumbnail((ANALYSIS_SIZE, ANALYSIS_SIZE))

        pixels = _pixels(gray)
        brightness = sum(pixels) / len(pixels)

        edge_pixels = _pixels(gray.filter(ImageFilter.FIND_EDGES))
        mean = sum(edge_pixels) / len(edge_pixels)
        edge_energy = (sum((p - mean) ** 2 for p in edge_pixels) / len(edge_pixels)) ** 0.5

        tiny = _pixels(gray.resize((8, 8), Image.LANCZOS))
        avg = sum(tiny) / 64
        ahash = int("".join("1" if p >= avg else "0" for p in tiny), 2)
        return {"brightness": brightness, "edge_energy": edge_energy, "ahash": ahash}
    except Exception:
        return None


def verify_completion_photo(before, after):
    """
    before: stats of the citizen's original photo (or None if there wasn't one)
    after:  stats of the worker's completion photo (or None if unreadable)
    Returns (verdict, score, reasons). Reasons are shown to worker AND admin.
    """
    if after is None:
        return "pending_review", 0, ["The photo could not be analysed automatically."]

    score, reasons = 100, []

    if after["brightness"] < DARK_LIMIT:
        score -= PENALTY_BAD_EXPOSURE
        reasons.append("Photo is very dark — the completed work can't be seen.")
    elif after["brightness"] > BRIGHT_LIMIT:
        score -= PENALTY_BAD_EXPOSURE
        reasons.append("Photo is blank or overexposed — the completed work can't be seen.")

    if after["edge_energy"] < MIN_EDGE_DETAIL:
        score -= PENALTY_NO_DETAIL
        reasons.append("Photo has almost no detail (blurry, or a flat empty surface).")

    if before is not None:
        distance = bin(before["ahash"] ^ after["ahash"]).count("1")
        if distance <= SAME_PHOTO_HASH_DISTANCE:
            score -= PENALTY_REUSED_PHOTO
            reasons.append("Looks nearly identical to the original report photo.")

    score = max(0, score)
    return ("auto_approved" if score >= APPROVE_THRESHOLD else "pending_review"), score, reasons
