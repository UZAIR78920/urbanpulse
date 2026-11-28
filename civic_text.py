"""Shared text normalization for civic model training and inference."""

import re


_DRAINAGE_TERMS = re.compile(r"\b(?:drainage|storm drain|catch basin)\b", re.IGNORECASE)


def classifier_text(text):
    if _DRAINAGE_TERMS.search(text):
        text += " catch basin sewer flooding"
    return text
