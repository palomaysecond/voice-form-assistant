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

# A field needs confirmation if EITHER stage is unsure:
LOW_CONFIDENCE = 0.70      # NLU side: Language entity score / keyword certainty
LOW_STT_CONFIDENCE = 0.50  # STT side: lowest word confidence inside the field's words
# A date that already passed this year (and so resolved to next year) is
# flagged for confirmation by capping its confidence below LOW_CONFIDENCE.
PAST_DATE_WINDOW_DAYS = 90
PAST_DATE_CONFIDENCE = 0.5

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
            field = {"value": parsed.date().isoformat(), "spoken": e["text"],
                     "source": "DateTime entity", "confidence": e["confidence"]}
            if _rolled_over_past_date(e["text"], parsed, now):
                # "by November 13th" said on November 23rd resolves to next
                # year. That is rarely what the caller meant, so ask.
                field["past_date"] = True
                field["confidence"] = min(field["confidence"], PAST_DATE_CONFIDENCE)
            return field
    return None


def _rolled_over_past_date(text, parsed, now):
    """True if the date was pushed to next year because this year's one has
    just passed (within PAST_DATE_WINDOW_DAYS)."""
    if (parsed.date() - now.date()).days <= 180:
        return False
    past = dateparser.parse(text, languages=["en"],
                            settings={"PREFER_DATES_FROM": "past", "RELATIVE_BASE": now})
    return bool(past) and 0 < (now.date() - past.date()).days <= PAST_DATE_WINDOW_DAYS


def _extract_request_type(transcript):
    text = transcript.lower()
    hits = [rtype for rtype, words in REQUEST_KEYWORDS.items()
            if any(w in text for w in words)]
    if not hits:
        return None
    matched = next(w for w in REQUEST_KEYWORDS[hits[0]] if w in text)
    # One clear match is trustworthy; several matches are ambiguous.
    return {"value": hits[0], "source": "keyword match", "spoken": matched,
            "confidence": 0.8 if len(hits) == 1 else 0.5,
            **({"alternatives": hits[1:]} if len(hits) > 1 else {})}


def _tokens(text):
    return re.findall(r"[a-z0-9']+", text.lower())


def span_stt_confidence(span_text, words):
    """Lowest Speech word confidence among the words that make up span_text.

    `words` is the /transcribe word list (lexical, lower-case, e.g. "tenth"
    where the display text says "10th"). Prefer a contiguous match; otherwise
    use whichever span tokens appear. Returns None if nothing matches or the
    SDK gave no word confidences.
    """
    span = _tokens(span_text)
    lex = [(w["word"].lower(), w.get("confidence")) for w in words]
    if not span or not lex:
        return None
    n = len(span)
    for i in range(len(lex) - n + 1):
        if [t for t, _ in lex[i:i + n]] == span:
            confs = [c for _, c in lex[i:i + n] if c is not None]
            return round(min(confs), 3) if confs else None
    confs = [c for t, c in lex if t in span and c is not None]
    return round(min(confs), 3) if confs else None


def _apply_stt_confidence(fields, words):
    """Carry Speech uncertainty into each field (it is lost after NER)."""
    for f in fields.values():
        if f is None:
            continue
        stt = span_stt_confidence(f.get("spoken", f["value"]), words)
        f["nlu_confidence"] = f["confidence"]
        f["stt_confidence"] = stt
        if stt is not None:
            f["confidence"] = round(min(f["confidence"], stt), 3)


def _needs_confirmation(field):
    stt = field.get("stt_confidence")
    nlu = field.get("nlu_confidence", field["confidence"])
    return nlu < LOW_CONFIDENCE or (stt is not None and stt < LOW_STT_CONFIDENCE)


def _article(word):
    return "an" if word[:1].lower() in "aeiou" else "a"


def build_confirmation(fields, missing, low=()):
    parts = []
    if fields.get("name"):
        parts.append(f"your name as {fields['name']['value']}")
    if fields.get("request_type"):
        rtype = fields["request_type"]["value"].replace("_", " ")
        parts.append(f"{_article(rtype)} {rtype} request")
    if fields.get("date"):
        # Read back the resolved date, not the raw phrase ("by November 13th",
        # "next Friday"), so the user can catch a wrong resolution.
        d = datetime.fromisoformat(fields["date"]["value"])
        parts.append(f"on {d:%A, %B} {d.day}, {d.year}")
    text = ("I have " + ", ".join(parts) + ".") if parts else "I did not catch any details."
    if missing:
        text += " Please tell me your " + " and ".join(m.replace("_", " ") for m in missing) + "."
    if "name" in low:
        text += " I'm not sure I heard your name correctly. Could you spell it for me?"
    if "date" in low and (fields.get("date") or {}).get("past_date"):
        text += " That date has already passed this year, so I set it to next year."
    other_low = [f.replace("_", " ") for f in low if f != "name"]
    if other_low:
        text += " Please confirm the " + " and ".join(other_low) + "."
    if not missing and not low:
        text += " Is that correct?"
    return text


def extract_fields(transcript, entities, now=None, words=None):
    """Return {fields, missing_fields, low_confidence_fields, confirmation_text}.

    Pass `words` (the Speech word list) when the text came from audio, so a
    misheard field is flagged even if Language is sure it is, say, a name.
    """
    fields = {
        "name": _extract_name(transcript, entities),
        "date": _extract_date(entities, now=now),
        "request_type": _extract_request_type(transcript),
    }
    if words:
        _apply_stt_confidence(fields, words)
    missing = [f for f in FORM_FIELDS if fields[f] is None]
    low = [f for f in FORM_FIELDS
           if fields[f] is not None and _needs_confirmation(fields[f])]
    return {
        "fields": fields,
        "missing_fields": missing,
        "low_confidence_fields": low,
        "complete": not missing and not low,
        "fields_correct_first_try": first_try_score(missing, low),
        "confirmation_text": build_confirmation(fields, missing, low),
    }


def first_try_score(missing, low, corrected=()):
    """Domain metric: share of form fields accepted on the first pass.

    A field counts if it was filled and not flagged for confirmation. Until
    /confirm exists (Track 1) this is the system's own estimate; /confirm will
    pass the fields the user actually corrected in `corrected`.
    """
    bad = set(missing) | set(low) | set(corrected)
    return round(sum(1 for f in FORM_FIELDS if f not in bad) / len(FORM_FIELDS), 3)
