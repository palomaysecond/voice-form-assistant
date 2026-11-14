"""Product logic — turn a transcript + Language entities into form fields.

Form: name, date, request_type. Pure functions (no Azure calls), so this file
is fully unit-testable. Track 1 (multi-turn follow-up questions) builds on
`missing_fields` and the per-field confidence.
"""
import re
from datetime import datetime

import dateparser

FORM_FIELDS = ["name", "date", "request_type"]

# request_type -> trigger words. Edit to match your form.
REQUEST_KEYWORDS = {
    "refund": ["refund", "money back", "reimburse"],
    "repair": ["repair", "fix", "broken", "not working"],
    "appointment": ["appointment", "schedule", "book", "meeting"],
    "account_change": ["update my", "change my", "address change", "account"],
    "complaint": ["complain", "complaint", "unhappy", "problem with"],
}

LOW_CONFIDENCE = 0.70  # below this a field is treated as "needs confirmation"

_NAME_PATTERN = re.compile(
    r"\b(?i:my name is|this is|i am|i'm)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})")


def _extract_name(transcript, entities):
    people = [e for e in entities if e["category"] == "Person"]
    if people:
        best = max(people, key=lambda e: e["confidence"])
        return {"value": best["text"], "source": "Person entity",
                "confidence": best["confidence"]}
    m = _NAME_PATTERN.search(transcript)
    if m:
        return {"value": m.group(1), "source": "phrase pattern", "confidence": 0.6}
    return None


def _extract_date(entities, now=None):
    now = now or datetime.now()
    settings = {"PREFER_DATES_FROM": "future", "RELATIVE_BASE": now}
    for e in entities:
        if e["category"] != "DateTime":
            continue
        if e.get("subcategory") not in (None, "Date", "DateTime", "DateRange"):
            continue
        parsed = dateparser.parse(e["text"], languages=["en"], settings=settings)
        if parsed:
            return {"value": parsed.date().isoformat(), "spoken": e["text"],
                    "source": "DateTime entity", "confidence": e["confidence"]}
    return None


def _extract_request_type(transcript):
    text = transcript.lower()
    hits = [rtype for rtype, words in REQUEST_KEYWORDS.items()
            if any(w in text for w in words)]
    if not hits:
        return None
    # One clear match is trustworthy; several matches are ambiguous.
    return {"value": hits[0], "source": "keyword match",
            "confidence": 0.8 if len(hits) == 1 else 0.5,
            **({"alternatives": hits[1:]} if len(hits) > 1 else {})}


def build_confirmation(fields, missing):
    parts = []
    if fields.get("name"):
        parts.append(f"your name as {fields['name']['value']}")
    if fields.get("request_type"):
        parts.append(f"a {fields['request_type']['value'].replace('_', ' ')} request")
    if fields.get("date"):
        parts.append(f"on {fields['date'].get('spoken', fields['date']['value'])}")
    text = ("I have " + ", ".join(parts) + ".") if parts else "I did not catch any details."
    if missing:
        text += " Please tell me your " + " and ".join(m.replace("_", " ") for m in missing) + "."
    else:
        text += " Is that correct?"
    return text


def extract_fields(transcript, entities, now=None):
    """Return {fields, missing_fields, low_confidence_fields, confirmation_text}."""
    fields = {
        "name": _extract_name(transcript, entities),
        "date": _extract_date(entities, now=now),
        "request_type": _extract_request_type(transcript),
    }
    missing = [f for f in FORM_FIELDS if fields[f] is None]
    low = [f for f in FORM_FIELDS
           if fields[f] is not None and fields[f]["confidence"] < LOW_CONFIDENCE]
    return {
        "fields": fields,
        "missing_fields": missing,
        "low_confidence_fields": low,
        "complete": not missing and not low,
        "confirmation_text": build_confirmation(fields, missing),
    }
