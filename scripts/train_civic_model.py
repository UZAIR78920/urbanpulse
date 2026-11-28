"""Download, curate, evaluate, and export a real-data civic issue classifier."""

import csv
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import accuracy_score, f1_score, classification_report
from sklearn.model_selection import GroupShuffleSplit
from sklearn.pipeline import make_pipeline
from sklearn.svm import LinearSVC

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from civic_text import classifier_text

DATA_DIR = ROOT / "data"
OUT_CSV = DATA_DIR / "civic_training_real.csv"
METRICS_PATH = DATA_DIR / "civic_model_metrics.json"
API = "https://data.cityofnewyork.us/resource/erm2-nwe9.json"
DATASET_PAGE = (
    "https://data.cityofnewyork.us/Social-Services/"
    "311-Service-Requests-from-2020-to-Present/erm2-nwe9"
)
SAMPLE_PER_PROBLEM = 800
PROBLEMS = (
    "Street Condition",
    "Sidewalk Condition",
    "Building/Use",
    "Water System",
    "PLUMBING",
    "WATER LEAK",
    "ELECTRIC",
    "Street Light Condition",
    "Sewer",
    "Sanitation Condition",
    "Dirty Condition",
    "Missed Collection",
    "Traffic Signal Condition",
    "Damaged Tree",
    "Bus Stop Shelter Placement",
    "Hazardous Materials",
    "Illegal Fireworks",
    "Illegal Parking",
    "Blocked Driveway",
)


def map_record(problem, detail, detail_2):
    p = (problem or "").strip().casefold()
    text = " ".join((detail or "", detail_2 or "")).casefold()

    if p == "street condition":
        return "Roads and Infrastructure", "Pothole" if "pothole" in text else "Road Damage"
    if p == "sidewalk condition":
        if "crosswalk" in text or "pedestrian" in text:
            return "Transportation", "Pedestrian Crossing"
        return "Roads and Infrastructure", "Road Damage"
    if p == "building/use":
        return "Roads and Infrastructure", "Illegal Construction"
    if p == "water system":
        if "dirty water" in text:
            return "Water Supply", "Contaminated Water"
        if any(term in text for term in ("leak", "hydrant running", "main break")):
            return "Water Supply", "Water Leakage"
        return "Water Supply", "Water Supply"
    if p == "plumbing":
        if "water supply" in text or "no water" in text:
            return "Water Supply", "Water Supply"
        if any(term in text for term in ("leak", "pipe", "water main")):
            return "Water Supply", "Water Leakage"
        return None
    if p == "water leak":
        return "Water Supply", "Water Leakage"
    if p == "electric":
        if "power outage" in text:
            return "Electricity", "Power Outage"
        if "lighting" in text or "light" in text:
            return "Electricity", "Streetlight"
        return "Electricity", "Electrical Hazard"
    if p == "street light condition":
        return "Electricity", "Streetlight"
    if p == "sewer":
        return "Sanitation", "Drainage"
    if p in ("sanitation condition", "dirty condition", "missed collection"):
        if any(term in text for term in ("toilet", "restroom")):
            return "Sanitation", "Public Toilets"
        return "Sanitation", "Garbage & Waste"
    if p == "traffic signal condition":
        if "ped" in text or "crosswalk" in text:
            return "Transportation", "Pedestrian Crossing"
        return "Transportation", "Traffic Signal"
    if p == "damaged tree":
        return "Public Safety", "Pedestrian Safety"
    if p == "hazardous materials":
        return "Public Safety", "Environmental Hazard"
    if p == "illegal fireworks":
        return "Public Safety", "Fire Hazard"
    if p in ("illegal parking", "blocked driveway"):
        if any(term in text for term in ("crosswalk", "sidewalk", "bike lane")):
            return "Transportation", "Pedestrian Crossing"
    return None


def fetch_problem(problem):
    params = {
        "$select": "complaint_type,descriptor,descriptor_2,location_type,created_date",
        "$where": (
            f"complaint_type='{problem}' AND descriptor IS NOT NULL "
            "AND created_date >= '2020-01-01T00:00:00'"
        ),
        "$order": "created_date DESC",
        "$limit": str(SAMPLE_PER_PROBLEM),
    }
    request = Request(
        API + "?" + urlencode(params),
        headers={"Accept": "application/json", "User-Agent": "CivicConnect-ML-Training/1.0"},
    )
    with urlopen(request, timeout=90) as response:
        rows = json.load(response)
    return rows


def prepare_records():
    unique = {}
    seen_sources = Counter()
    dropped = Counter()
    for problem in PROBLEMS:
        try:
            rows = fetch_problem(problem)
        except Exception as error:
            raise RuntimeError(f"Could not download NYC 311 records for {problem!r}: {error}") from error
        seen_sources[problem] = len(rows)
        for row in rows:
            detail = (row.get("descriptor") or "").strip()
            detail_2 = (row.get("descriptor_2") or "").strip()
            location_type = (row.get("location_type") or "").strip()
            labels = map_record(row.get("complaint_type"), detail, detail_2)
            if not labels or not detail or detail.casefold() in {"n/a", "other"}:
                dropped[problem] += 1
                continue
            # Use only the source's controlled problem-detail fields as model text.
            # Do not export identifiers, addresses, coordinates, or resolutions.
            feature_text = " ".join(part for part in (detail, detail_2, location_type) if part)
            key = (feature_text.casefold(), labels[0], labels[1])
            record = {
                "problem": row.get("complaint_type", ""),
                "problem_detail": detail,
                "additional_detail": detail_2,
                "location_type": location_type,
                "category_group": labels[0],
                "subcategory": labels[1],
                "created_date": row.get("created_date", ""),
            }
            if key not in unique:
                unique[key] = record

    records = sorted(
        unique.values(),
        key=lambda item: (
            item["category_group"], item["subcategory"], item["problem"],
            item["problem_detail"], item["additional_detail"], item["location_type"],
        ),
    )
    if len(records) < 60:
        raise RuntimeError(f"Only {len(records)} usable unique records were downloaded.")
    if len({item["category_group"] for item in records}) < 4:
        raise RuntimeError("The downloaded dataset did not cover enough civic categories.")
    return records, seen_sources, dropped


def record_text(record):
    return classifier_text(
        " ".join(
            part for part in (
                record["problem_detail"], record["additional_detail"], record["location_type"]
            ) if part
        )
    )


def evaluate(records):
    texts = [record_text(item) for item in records]
    categories = [item["category_group"] for item in records]
    subcategories = [item["subcategory"] for item in records]
    groups = [re.sub(r"\s+", " ", text.casefold()).strip() for text in texts]
    splitter = GroupShuffleSplit(n_splits=1, test_size=0.25, random_state=42)
    train_indexes, test_indexes = next(splitter.split(texts, categories, groups))

    def make_text_model():
        return make_pipeline(
            TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True),
            LinearSVC(class_weight="balanced", random_state=42),
        )

    def report(targets, predicted, train_count):
        test_y = [targets[index] for index in test_indexes]
        return {
            "accuracy": round(float(accuracy_score(test_y, predicted)), 4),
            "macro_f1": round(float(f1_score(test_y, predicted, average="macro", zero_division=0)), 4),
            "report": classification_report(
                test_y, predicted, output_dict=True, zero_division=0
            ),
            "train_records": train_count,
            "test_records": len(test_indexes),
            "test_labels": sorted(set(test_y)),
        }

    category_model = make_text_model()
    category_model.fit([texts[index] for index in train_indexes],
                       [categories[index] for index in train_indexes])
    category_predictions = category_model.predict([texts[index] for index in test_indexes])

    subcategory_models = {}
    for category in sorted(set(categories[index] for index in train_indexes)):
        category_indexes = [index for index in train_indexes if categories[index] == category]
        labels = [subcategories[index] for index in category_indexes]
        if len(set(labels)) == 1:
            subcategory_models[category] = labels[0]
        else:
            model = make_text_model()
            model.fit([texts[index] for index in category_indexes], labels)
            subcategory_models[category] = model

    subcategory_predictions = []
    for index, predicted_category in zip(test_indexes, category_predictions):
        model = subcategory_models[predicted_category]
        if isinstance(model, str):
            subcategory_predictions.append(model)
        else:
            subcategory_predictions.append(model.predict([texts[index]])[0])

    return {
        "split": "75/25 GroupShuffleSplit by normalized model text, random_state=42",
        "category_group": report(categories, category_predictions, len(train_indexes)),
        "subcategory_end_to_end": report(
            subcategories, subcategory_predictions, len(train_indexes)
        ),
    }


def write_outputs(records, metrics, seen_sources, dropped):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as target:
        columns = (
            "problem", "problem_detail", "additional_detail", "location_type",
            "category_group", "subcategory", "created_date",
        )
        writer = csv.DictWriter(target, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)

    metadata = {
        "dataset_name": "NYC 311 Service Requests from 2020 to Present",
        "dataset_id": "erm2-nwe9",
        "source_url": DATASET_PAGE,
        "api_url": API,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_fields": [
            "complaint_type", "descriptor", "descriptor_2", "location_type", "created_date"
        ],
        "target_mapping": "Curated rules in scripts/train_civic_model.py; NYC service request labels mapped to CivicConnect categories.",
        "excluded_fields": ["unique_key", "addresses", "coordinates", "resolution_description"],
        "source_requests_per_problem": dict(seen_sources),
        "dropped_unmapped_or_generic_records": dict(dropped),
        "unique_training_records": len(records),
        "category_distribution": dict(Counter(item["category_group"] for item in records)),
        "subcategory_distribution": dict(Counter(item["subcategory"] for item in records)),
        "evaluation": metrics,
        "limitations": [
            "The source contains structured Problem and Problem Detail labels, not citizen-written narratives.",
            "Target labels are a documented mapping from NYC categories to the app's local civic taxonomy.",
            "Severity is not labeled in this source and is not trained by this model.",
            "The NYC label distribution and terminology may not represent the deployment jurisdiction.",
        ],
    }
    METRICS_PATH.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main():
    print("Downloading public NYC 311 service request label records...")
    records, seen_sources, dropped = prepare_records()
    print(f"Prepared {len(records)} unique real source records; evaluating grouped holdout...")
    metrics = evaluate(records)
    write_outputs(records, metrics, seen_sources, dropped)
    print(json.dumps({
        "records": len(records),
        "categories": metrics["category_group"]["accuracy"],
        "category_macro_f1": metrics["category_group"]["macro_f1"],
        "subcategory_end_to_end": metrics["subcategory_end_to_end"]["accuracy"],
        "subcategory_macro_f1": metrics["subcategory_end_to_end"]["macro_f1"],
        "training_file": str(OUT_CSV),
        "metrics_file": str(METRICS_PATH),
    }, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Training failed: {error}", file=sys.stderr)
        raise
