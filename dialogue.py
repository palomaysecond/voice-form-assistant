"""Track 1 — confidence-aware multi-turn correction (pure logic, no Azure).

/fill-form opens a session from the first-pass form. Each /confirm reply is
parsed here into: values the user supplied (a spelled name "J-A-C-K", a
correction "no, Tuesday", a missing field), a yes, or a no. The app asks only
about fields that are still missing or uncertain, and the form is complete
only after the user says yes to a read-back.

Domain metric at completion: share of fields the user accepted without
correcting them (fields that were missing at first count as not correct).
"""
import re
import uuid
from datetime import datetime, timezone

from form_extractor import (FORM_FIELDS, _NAME_PATTERN, _apply_stt_confidence,
                            _extract_date, _extract_request_type,
                            _needs_confirmation, build_confirmation,
                            first_try_score)

AFFIRM_WORDS = {"yes", "yeah", "yep", "yup", "correct", "right", "sure",
                "ok", "okay", "confirm", "confirmed", "perfect", "exactly"}
AFFIRM_PHRASES = ("that's right", "that is right", "sounds good", "looks good",
                  "that's correct", "that is correct")
NEGATE_WORDS = {"no", "nope", "wrong", "incorrect", "nah"}
NEGATE_PHRASES = ("that's not", "that is not", "not right", "not correct")
# Words that can surround a bare name reply ("It's Jack Carter, please").
_FILLER = {"it's", "its", "it", "is", "my", "name", "the", "please", "um",
           "uh", "i", "said", "and", "sorry"}


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _words(text):
    return re.findall(r"[a-z']+", text.lower())


def detect_yes_no(text):
    low = text.lower()
    words = set(_words(text))
    affirm = bool(words & AFFIRM_WORDS) or any(p in low for p in AFFIRM_PHRASES)
    negate = bool(words & NEGATE_WORDS) or any(p in low for p in NEGATE_PHRASES)
    if affirm and negate:  # "no, that's right" is too ambiguous to act on
        return False, True
    return affirm, negate


def parse_spelled(text):
    """Spelled words in a reply: "J-A-C-K", "J.A.C.K." or "J A C K" -> ["Jack"].

    A run of two or more single letters is one word (commas between letters
    are fine: "J, A, C, K"); any longer word ends the run.
    """
    words, run = [], []

    def flush():
        if len(run) >= 2:
            words.append("".join(run).capitalize())
        run.clear()

    for raw in re.split(r"\s+", text.strip()):
        tok = raw.strip(",;:!?\"'")
        # "J, A, C, K" keeps going; a comma after a whole word ends the run.
        ends_group = raw[-1:] in ",;" and len(tok.rstrip(".")) > 1
        if re.fullmatch(r"[A-Za-z](?:[-.][A-Za-z])+\.?", tok):
            flush()
            run.extend(re.findall(r"[A-Za-z]", tok))
            flush()
        elif re.fullmatch(r"[A-Za-z]\.?", tok):
            run.append(tok[0])
        else:
            flush()
        if ends_group:
            flush()
    flush()
    return words


def spelled_from_speech(words, text):
    """Spelled name from Speech output, or [] if the reply was not spelled.

    Azure Speech joins spelled letters in the display text ("JACKCARTER." or,
    with a pause, "Jack. Carter.") but keeps them as single letters in the
    lexical word list. So: find the letter run in `words`, then use the
    display words only to split it into first / last name.
    """
    run, best = [], []
    for w in words or []:
        token = w["word"].strip(".")
        if len(token) == 1 and token.isalpha():
            run.append(token.lower())
        else:
            best, run = max(best, run, key=len), []
    run = max(best, run, key=len)
    if len(run) < 2:
        return []
    letters = "".join(run)
    display = re.findall(r"[A-Za-z]+", text)
    for i in range(len(display)):
        joined = ""
        for j in range(i, len(display)):
            joined += display[j].lower()
            if joined == letters:
                return [d.capitalize() for d in display[i:j + 1]]
            if not letters.startswith(joined):
                break
    return [letters.capitalize()]


def _name_from_reply(text, entities, name_pending, words=None):
    spelled = spelled_from_speech(words, text) or parse_spelled(text)
    if spelled:
        # The user spelled it on purpose, so letter-level Speech confidence
        # does not re-flag it; the final read-back + "yes" still guards it.
        return {"value": " ".join(spelled[:3]), "source": "spelled by user",
                "confidence": 1.0, "spelled": True}
    people = [e for e in entities if e["category"] == "Person"]
    if people:
        best = max(people, key=lambda e: e["confidence"])
        return {"value": best["text"], "source": "Person entity (reply)",
                "confidence": best["confidence"]}
    m = _NAME_PATTERN.search(text)
    if m:
        return {"value": m.group(1), "source": "phrase pattern (reply)",
                "confidence": 0.6}
    if name_pending:
        # We just asked for the name, so a short bare answer is the name.
        bare = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]*", text)
                if w.lower() not in _FILLER]
        if 1 <= len(bare) <= 3 and not any(
                w.lower() in AFFIRM_WORDS | NEGATE_WORDS for w in bare):
            return {"value": " ".join(w.capitalize() for w in bare),
                    "source": "answer to name question", "confidence": 0.8}
    return None


def parse_reply(text, entities, words=None, pending=(), now=None):
    """Return (updates, affirm, negate) for one user reply."""
    affirm, negate = detect_yes_no(text)
    updates = {}
    name = _name_from_reply(text, entities, "name" in pending, words)
    if name:
        updates["name"] = name
    date = _extract_date(entities, now=now)
    if date:
        updates["date"] = date
    rtype = _extract_request_type(text)
    if rtype:
        updates["request_type"] = rtype
    if words:
        # Keep the Speech confidence check from the first pass, except for a
        # spelled name: its letters never match the name as one word.
        _apply_stt_confidence({k: v for k, v in updates.items()
                               if not v.get("spelled")}, words)
    for v in updates.values():
        v.setdefault("nlu_confidence", v["confidence"])
        v.setdefault("stt_confidence", None)
    return updates, affirm, negate


def _pending(fields):
    missing = [f for f in FORM_FIELDS if not fields.get(f)]
    low = [f for f in FORM_FIELDS if fields.get(f)
           and not fields[f].get("user_confirmed")
           and _needs_confirmation(fields[f])]
    return missing, low


def new_session(form, voice):
    """Session document for Cosmos DB, built from the /fill-form result."""
    fields = form["fields"]
    missing, low = _pending(fields)
    return {
        "id": uuid.uuid4().hex,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "voice": voice,
        "turn": 0,
        "status": "collecting" if (missing or low) else "confirming",
        "fields": fields,
        "initial_values": {f: (fields[f] or {}).get("value") for f in FORM_FIELDS},
        "initial_missing": missing,
        "corrected_fields": [],
        "missing_fields": missing,
        "low_confidence_fields": low,
        "first_try_estimate": form["fields_correct_first_try"],
        "fields_correct_first_try": None,  # set when the user confirms
        "confirmation_text": form["confirmation_text"],
        "history": [],
    }


def apply_reply(session, text, entities, words=None, now=None):
    """Update the session with one reply. Returns (session, action)."""
    if session["status"] == "complete":
        session["confirmation_text"] = "This form is already confirmed. Thank you!"
        return session, "already_complete"

    fields = session["fields"]
    pending = session["missing_fields"] + session["low_confidence_fields"]
    updates, affirm, negate = parse_reply(text, entities, words, pending, now)

    changed = []
    for name, new in updates.items():
        old = fields.get(name)
        if old and old.get("value") == new["value"]:
            # The user repeated the same value: that confirms it.
            old["user_confirmed"] = True
            continue
        if session["initial_values"].get(name) is not None \
                and name not in session["corrected_fields"]:
            session["corrected_fields"].append(name)
        new["user_supplied"] = True
        fields[name] = new
        changed.append(name)

    if affirm and not changed:
        for f in session["low_confidence_fields"]:
            if fields.get(f):
                fields[f]["user_confirmed"] = True

    missing, low = _pending(fields)
    session["missing_fields"], session["low_confidence_fields"] = missing, low
    session["turn"] += 1

    if affirm and not changed and not missing and not low:
        session["status"] = "complete"
        session["fields_correct_first_try"] = first_try_score(
            session["initial_missing"], [], session["corrected_fields"])
        session["confirmation_text"] = (
            build_confirmation(fields, [], []).replace(" Is that correct?", "")
            + " Your request is confirmed. Thank you!")
        action = "confirmed"
    elif changed:
        session["status"] = "collecting" if (missing or low) else "confirming"
        session["confirmation_text"] = "Thanks. " + build_confirmation(fields, missing, low)
        action = "updated"
    elif negate:
        session["status"] = "collecting"
        session["confirmation_text"] = (
            "Sorry about that. Which detail should I change: "
            "your name, the date, or the request type?")
        action = "asked_what_to_change"
    else:
        session["confirmation_text"] = (
            "Sorry, I didn't catch that. " + build_confirmation(fields, missing, low))
        action = "not_understood"

    session["history"].append({"turn": session["turn"], "action": action,
                               "updated_fields": changed, "at": _now_iso()})
    session["updated_at"] = _now_iso()
    return session, action
