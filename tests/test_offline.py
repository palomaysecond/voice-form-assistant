"""Offline tests — no Azure keys needed. Run: pytest -q"""
import io
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
from form_extractor import extract_fields  # noqa: E402

NOW = datetime(2026, 11, 14)


def ent(text, category, subcategory=None, confidence=0.95):
    return {"text": text, "category": category,
            "subcategory": subcategory, "confidence": confidence}


def test_all_fields_found():
    t = "My name is Jane Doe, I need a refund on December 3rd."
    out = extract_fields(t, [ent("Jane Doe", "Person"),
                             ent("December 3rd", "DateTime", "Date")], now=NOW)
    assert out["fields"]["name"]["value"] == "Jane Doe"
    assert out["fields"]["date"]["value"] == "2026-12-03"
    assert out["fields"]["request_type"]["value"] == "refund"
    assert out["complete"] is True
    assert out["confirmation_text"].endswith("Is that correct?")


def test_missing_date_asks_for_it():
    out = extract_fields("Hi, this is Tom Lee and my laptop is broken.",
                         [ent("Tom Lee", "Person")], now=NOW)
    assert out["missing_fields"] == ["date"]
    assert out["fields"]["request_type"]["value"] == "repair"
    assert "Please tell me your date" in out["confirmation_text"]


def test_relative_date_and_name_fallback():
    out = extract_fields("I'm Maria Garcia and I want to book an appointment next Friday",
                         [ent("next Friday", "DateTime", "Date")], now=NOW)
    assert out["fields"]["name"]["source"] == "phrase pattern"
    assert out["fields"]["date"]["value"] == "2026-11-20"
    assert "name" in out["low_confidence_fields"]  # pattern match = 0.6


def test_transcribe_requires_file():
    client = app_module.app.test_client()
    r = client.post("/transcribe", data={})
    assert r.status_code == 400


def test_transcribe_rejects_unsupported_type():
    client = app_module.app.test_client()
    r = client.post("/transcribe",
                    data={"audio": (io.BytesIO(b"hello"), "notes.txt")},
                    content_type="multipart/form-data")
    assert r.status_code == 415


# Word list shaped like real /transcribe output for recording T1
# (spoken "Jack Carter", heard "Jet Tutor").
T1_WORDS = [{"word": w, "confidence": c} for w, c in [
    ("hi", 0.89), ("my", 0.79), ("name", 0.89), ("is", 0.39), ("jet", 0.09),
    ("tutor", 0.05), ("i'd", 0.43), ("like", 0.56), ("to", 0.64),
    ("schedule", 0.54), ("an", 0.50), ("appointment", 0.62), ("on", 0.68),
    ("december", 0.61), ("tenth", 0.45)]]
T1_TEXT = "Hi my name is Jet Tutor. I'd like to schedule an appointment on December 10th."
T1_ENTITIES = [ent("Jet Tutor", "Person", confidence=1.0),
               ent("December 10th", "DateTime", "Date", confidence=1.0)]


def test_misheard_name_is_flagged_despite_confident_ner():
    out = extract_fields(T1_TEXT, T1_ENTITIES, now=NOW, words=T1_WORDS)
    name = out["fields"]["name"]
    assert name["nlu_confidence"] == 1.0
    assert name["stt_confidence"] == 0.05
    assert name["confidence"] == 0.05
    assert out["low_confidence_fields"] == ["name"]
    assert out["complete"] is False
    assert "spell it" in out["confirmation_text"]


def test_lexical_form_mismatch_uses_matching_tokens():
    # "10th" is "tenth" in the lexical word list; only "december" matches.
    out = extract_fields(T1_TEXT, T1_ENTITIES, now=NOW, words=T1_WORDS)
    assert out["fields"]["date"]["stt_confidence"] == 0.61
    assert out["fields"]["request_type"]["stt_confidence"] == 0.62  # "appointment"
    assert "date" not in out["low_confidence_fields"]


def test_without_words_behaviour_is_unchanged():
    out = extract_fields(T1_TEXT, T1_ENTITIES, now=NOW)
    assert "stt_confidence" not in out["fields"]["name"]
    assert out["complete"] is True
