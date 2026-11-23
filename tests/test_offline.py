"""Offline tests — no Azure keys needed. Run: pytest -q"""
import io
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["TELEMETRY_DISABLED"] = "1"  # never send test runs to App Insights

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


# --- Part D: summary + TTS (no Azure calls) ---------------------------------
import pytest  # noqa: E402

import telemetry  # noqa: E402
import tts_service  # noqa: E402
from form_extractor import first_try_score  # noqa: E402

LANG = {"entities": T1_ENTITIES, "key_phrases": ["appointment", "December"],
        "sentiment": {"label": "neutral"}}


def test_analysis_summary_has_phrases_sentiment_and_entity_types():
    s = tts_service.build_analysis_summary(LANG)
    assert "2 key phrases" in s and '"appointment"' in s
    assert "tone was neutral" in s
    assert "1 person and 1 date" in s


def test_ssml_escapes_text_and_uses_voice():
    ssml = tts_service.build_ssml("Tom & <Jerry>", "formal")
    assert "Tom &amp; &lt;Jerry&gt;" in ssml
    assert "en-US-GuyNeural" in ssml


def test_unknown_voice_rejected():
    with pytest.raises(ValueError):
        tts_service.resolve_voice("robot")
    assert tts_service.resolve_voice(None) == tts_service.DEFAULT_VOICE


def test_first_try_score():
    assert first_try_score([], []) == 1.0
    assert first_try_score(["date"], ["name"]) == 0.333
    assert first_try_score([], [], corrected=["name"]) == 0.667


# --- Full pipeline with the three Azure calls stubbed -----------------------
def _stub_pipeline(monkeypatch, tts_fails=False):
    monkeypatch.setattr(app_module, "transcribe", lambda p: {
        "transcript": T1_TEXT, "language": "en-US", "duration_seconds": 4.2,
        "confidence": 0.61, "words": T1_WORDS})
    monkeypatch.setattr(app_module, "analyze", lambda t: LANG)

    def fake_tts(text, voice):
        if tts_fails:
            raise app_module.SpeechServiceError("TTS down")
        return {"audio_base64": "AAAA", "mime_type": "audio/mp3", "voice": voice,
                "voice_name": "en-US-AriaNeural", "char_count": len(text)}
    monkeypatch.setattr(app_module, "synthesize", fake_tts)


def _post_audio(client, url="/process", **extra):
    return client.post(url, data={"audio": (io.BytesIO(b"RIFF"), "t1.wav"), **extra},
                       content_type="multipart/form-data")


def test_process_returns_form_summary_and_audio(monkeypatch):
    _stub_pipeline(monkeypatch)
    r = _post_audio(app_module.app.test_client(), voice="lively")
    assert r.status_code == 200
    body = r.get_json()
    assert body["low_confidence_fields"] == ["name"]
    assert body["fields_correct_first_try"] == 0.667
    assert body["audio"]["voice"] == "lively"
    assert body["summary_text"].startswith(body["confirmation_text"])
    assert set(body["timings_ms"]) == {"stt_ms", "language_ms", "tts_ms"}
    assert telemetry.telemetry_summary()["successful_calls"] >= 1


def test_process_rejects_unknown_voice(monkeypatch):
    _stub_pipeline(monkeypatch)
    r = _post_audio(app_module.app.test_client(), "/fill-form", voice="robot")
    assert r.status_code == 400


def test_tts_failure_is_502_and_logged_by_stage(monkeypatch):
    _stub_pipeline(monkeypatch, tts_fails=True)
    r = _post_audio(app_module.app.test_client())
    assert r.status_code == 502
    assert telemetry.telemetry_summary()["errors_by_stage"]["text_to_speech"] >= 1


def test_voices_lists_three():
    r = app_module.app.test_client().get("/voices")
    assert [v["key"] for v in r.get_json()] == ["formal", "casual", "lively"]


def test_index_page_served():
    r = app_module.app.test_client().get("/")
    assert r.status_code == 200 and b"Voice Form Assistant" in r.data
