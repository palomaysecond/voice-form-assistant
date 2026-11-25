"""Track 1 offline tests: multi-turn correction logic and /confirm."""
import io
from datetime import datetime

import pytest

import app as app_module
import session_store
from dialogue import apply_reply, new_session, parse_spelled
from form_extractor import extract_fields
from test_offline import LANG, T1_ENTITIES, T1_TEXT, T1_WORDS, ent

NOW = datetime(2026, 11, 14)


def t1_session():
    return new_session(extract_fields(T1_TEXT, T1_ENTITIES, now=NOW, words=T1_WORDS),
                       "casual")


@pytest.mark.parametrize("text,expected", [
    ("J-A-C-K", ["Jack"]),
    ("J.A.C.K.", ["Jack"]),
    ("J A C K.", ["Jack"]),
    ("J, A, C, K.", ["Jack"]),
    ("It's J-A-C-K C-A-R-T-E-R", ["Jack", "Carter"]),
    ("I need a refund", []),
    ("Yes.", []),
])
def test_parse_spelled(text, expected):
    assert parse_spelled(text) == expected


def test_new_session_waits_for_misheard_name():
    s = t1_session()
    assert s["status"] == "collecting"
    assert s["low_confidence_fields"] == ["name"]
    assert s["initial_values"]["name"] == "Jet Tutor"


def test_spelled_name_then_yes_completes_with_real_metric():
    s = t1_session()
    s, action = apply_reply(s, "J-A-C-K C-A-R-T-E-R", [], now=NOW)
    assert action == "updated"
    assert s["fields"]["name"]["value"] == "Jack Carter"
    assert s["fields"]["name"]["source"] == "spelled by user"
    assert s["corrected_fields"] == ["name"]
    assert s["status"] == "confirming"
    assert s["confirmation_text"].endswith("Is that correct?")
    s, action = apply_reply(s, "Yes, that's right.", [], now=NOW)
    assert action == "confirmed" and s["status"] == "complete"
    assert s["fields_correct_first_try"] == 0.667  # name was corrected
    assert s["turn"] == 2


def test_yes_accepts_a_flagged_field_that_was_right():
    s, action = apply_reply(t1_session(), "Yes.", [], now=NOW)
    assert action == "confirmed"
    assert s["fields_correct_first_try"] == 1.0


def test_date_correction_no_tuesday():
    s = t1_session()
    s, _ = apply_reply(s, "J-A-C-K", [], now=NOW)
    s, action = apply_reply(s, "No, Tuesday.", [ent("Tuesday", "DateTime", "Date")], now=NOW)
    assert action == "updated"
    assert s["fields"]["date"]["value"] == "2026-11-17"
    assert s["corrected_fields"] == ["name", "date"]


def test_missing_field_is_filled_and_not_counted_as_correction():
    text = "Hi, my name is Jack Carter and I need a refund."
    form = extract_fields(text, [ent("Jack Carter", "Person")], now=NOW)
    s = new_session(form, "casual")
    assert s["missing_fields"] == ["date"]
    s, _ = apply_reply(s, "December 3rd.", [ent("December 3rd", "DateTime", "Date")], now=NOW)
    assert s["missing_fields"] == [] and s["corrected_fields"] == []
    s, action = apply_reply(s, "Yes", [], now=NOW)
    assert action == "confirmed"
    assert s["fields_correct_first_try"] == 0.667  # date was missing at first


def test_bare_name_answer_when_name_was_asked():
    s, _ = apply_reply(t1_session(), "Jack Carter", [], now=NOW)
    assert s["fields"]["name"]["value"] == "Jack Carter"


def test_plain_no_asks_what_to_change():
    s, action = apply_reply(t1_session(), "No.", [], now=NOW)
    assert action == "asked_what_to_change" and s["status"] == "collecting"


def test_unclear_reply_repeats_the_question():
    s = t1_session()
    s["status"], s["low_confidence_fields"] = "confirming", []
    s["fields"]["name"]["user_confirmed"] = True
    s, action = apply_reply(s, "Hmm.", [], now=NOW)
    assert action == "not_understood"
    assert s["confirmation_text"].startswith("Sorry, I didn't catch that.")


def test_reply_to_completed_session():
    s, _ = apply_reply(t1_session(), "Yes", [], now=NOW)
    s, action = apply_reply(s, "Yes", [], now=NOW)
    assert action == "already_complete"


# --- Endpoints with Azure stubbed --------------------------------------------
@pytest.fixture
def client(monkeypatch):
    session_store.set_store(session_store.InMemorySessionStore())
    monkeypatch.setattr(app_module, "transcribe", lambda p: {
        "transcript": T1_TEXT, "language": "en-US", "duration_seconds": 4.2,
        "confidence": 0.61, "words": T1_WORDS})
    replies = {"J-A-C-K C-A-R-T-E-R": [], "Yes": []}
    monkeypatch.setattr(app_module, "analyze",
                        lambda t: LANG if t == T1_TEXT else
                        {**LANG, "entities": replies.get(t, [])})
    monkeypatch.setattr(app_module, "synthesize", lambda text, voice: {
        "audio_base64": "AAAA", "mime_type": "audio/mp3", "voice": voice,
        "voice_name": "en-US-JennyNeural", "char_count": len(text)})
    return app_module.app.test_client()


def test_fill_form_then_confirm_over_http(client):
    r = client.post("/fill-form", data={"audio": (io.BytesIO(b"RIFF"), "t1.wav")},
                    content_type="multipart/form-data")
    sid = r.get_json()["session_id"]
    assert sid

    r = client.post("/confirm", json={"session_id": sid, "text": "J-A-C-K C-A-R-T-E-R"})
    body = r.get_json()
    assert r.status_code == 200 and body["action"] == "updated"
    assert body["fields"]["name"]["value"] == "Jack Carter"
    assert body["audio"]["audio_base64"] == "AAAA"
    assert {"session_load_ms", "language_ms", "session_save_ms", "tts_ms"} <= set(body["timings_ms"])

    r = client.post("/confirm", data={"session_id": sid, "text": "Yes"})
    body = r.get_json()
    assert body["complete"] is True and body["fields_correct_first_try"] == 0.667

    stored = client.get(f"/sessions/{sid}").get_json()
    assert stored["status"] == "complete" and stored["corrected_fields"] == ["name"]


def test_confirm_with_audio_reply(client, monkeypatch):
    sid = client.post("/fill-form", data={"audio": (io.BytesIO(b"RIFF"), "t1.wav")},
                      content_type="multipart/form-data").get_json()["session_id"]
    monkeypatch.setattr(app_module, "transcribe", lambda p: {
        "transcript": "Yes", "language": "en-US", "duration_seconds": 1.0,
        "confidence": 0.95, "words": [{"word": "yes", "confidence": 0.95}]})
    r = client.post("/confirm", data={"session_id": sid,
                                      "audio": (io.BytesIO(b"RIFF"), "reply.webm")},
                    content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["complete"] is True
    assert "stt_ms" in r.get_json()["timings_ms"]


def test_confirm_errors(client):
    assert client.post("/confirm", json={"text": "yes"}).status_code == 400
    assert client.post("/confirm", json={"session_id": "abc"}).status_code == 400
    assert client.post("/confirm", json={"session_id": "nope", "text": "yes"}).status_code == 404
    assert client.get("/sessions/nope").status_code == 404


def _letters(s, conf=0.5):
    return [{"word": c, "confidence": conf} for c in s]


def test_spelled_name_from_speech_word_list():
    from dialogue import spelled_from_speech
    # Real Azure output for "J, A, C, K, C, A, R, T, E, R" with and without a pause
    assert spelled_from_speech(_letters("JACKCARTER"), "JACKCARTER.") == ["Jackcarter"]
    assert spelled_from_speech(_letters("JACKCARTER"), "Jack. Carter.") == ["Jack", "Carter"]
    assert spelled_from_speech([{"word": "yes", "confidence": 0.9}], "Yes.") == []


def test_spelled_audio_reply_is_not_reflagged_by_low_letter_confidence():
    s, _ = apply_reply(t1_session(), "Jack. Carter.", [], words=_letters("JACKCARTER", 0.16), now=NOW)
    name = s["fields"]["name"]
    assert name["value"] == "Jack Carter" and name["source"] == "spelled by user"
    assert s["low_confidence_fields"] == [] and s["status"] == "confirming"
