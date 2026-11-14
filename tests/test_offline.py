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
