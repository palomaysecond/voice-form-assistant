"""Part H option 2: confidence-based retry, SQLite log and GET /stats."""
import io

import pytest

import app as app_module
import stats_store
from confidence_retry import low_confidence_words, transcribe_with_retry

WORDS = [{"word": "jet", "confidence": 0.046, "offset": 1.2},
         {"word": "tutor", "confidence": 0.31, "offset": 1.5},
         {"word": "refund", "confidence": 0.93, "offset": 2.0},
         {"word": "maybe", "confidence": None}]


def fake_stt(conf_by_lang, calls=None, fail=()):
    def fn(path, language="en-US"):
        if calls is not None:
            calls.append(language)
        if language in fail:
            raise RuntimeError("throttled")
        return {"transcript": f"text {language}", "language": language,
                "duration_seconds": 3.0, "confidence": conf_by_lang[language],
                "words": WORDS}
    return fn


def test_low_confidence_words_skips_missing_scores():
    flagged = low_confidence_words(WORDS)
    assert [w["word"] for w in flagged] == ["jet", "tutor"]


def test_high_confidence_is_not_retried():
    calls = []
    out = transcribe_with_retry(fake_stt({"en-US": 0.9}, calls), "a.wav")
    assert calls == ["en-US"]
    assert out["retry"]["retried"] is False and out["language"] == "en-US"


def test_low_confidence_retries_and_keeps_the_better_result():
    calls = []
    out = transcribe_with_retry(fake_stt({"en-US": 0.61, "en-GB": 0.72}, calls), "a.wav",
                                retry_languages=("en-GB",))
    assert calls == ["en-US", "en-GB"]
    assert out["language"] == "en-GB" and out["confidence"] == 0.72
    r = out["retry"]
    assert r["retried"] and r["improved"] and r["primary_confidence"] == 0.61
    assert r["attempts"] == [{"language": "en-US", "confidence": 0.61},
                             {"language": "en-GB", "confidence": 0.72}]
    assert len(out["low_confidence_words"]) == 2


def test_default_tries_en_gb_then_en_in_and_keeps_the_best():
    calls = []
    out = transcribe_with_retry(
        fake_stt({"en-US": 0.54, "en-GB": 0.54, "en-IN": 0.69}, calls), "a.wav")
    assert calls == ["en-US", "en-GB", "en-IN"]
    assert out["language"] == "en-IN" and out["retry"]["improved"] is True


def test_worse_retry_keeps_the_first_result():
    out = transcribe_with_retry(fake_stt({"en-US": 0.61, "en-GB": 0.55, "en-IN": 0.6}), "a.wav")
    assert out["language"] == "en-US" and out["retry"]["improved"] is False


def test_failed_retry_never_fails_the_request():
    out = transcribe_with_retry(fake_stt({"en-US": 0.61}, fail=("en-GB",)), "a.wav",
                                retry_languages=("en-GB",))
    assert out["language"] == "en-US"
    assert out["retry"]["attempts"][1]["error"] == "throttled"


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("STATS_DB_PATH", str(tmp_path / "stats.db"))


def test_stats_summarizes_logged_transcriptions(fresh_db):
    stats_store.record(transcribe_with_retry(fake_stt({"en-US": 0.61, "en-GB": 0.72}), "a",
                                             retry_languages=("en-GB",)),
                       audio_format="m4a")
    stats_store.record(transcribe_with_retry(fake_stt({"en-US": 0.9}), "b"),
                       audio_format="webm")
    s = stats_store.summary()
    assert s["transcriptions"] == 2
    assert s["confidence"]["avg_final"] == 0.81
    assert s["confidence"]["avg_primary"] == 0.755
    assert s["confidence"]["below_alert_threshold"] == 1
    assert s["retry"] == {"retried": 1, "retry_rate": 0.5, "improved": 1,
                          "avg_gain_when_improved": 0.11,
                          "chosen_language": {"en-GB": 1, "en-US": 1}}
    assert s["low_confidence_words"]["most_common"][0] == ("jet", 2)
    assert s["recent"][0]["audio_format"] == "webm"  # newest first


def test_process_logs_retry_and_stats_endpoint(fresh_db, monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "transcribe",
                        fake_stt({"en-US": 0.61, "en-GB": 0.61, "en-IN": 0.66}, calls))
    monkeypatch.setattr(app_module, "analyze", lambda t: {
        "entities": [], "key_phrases": [],
        "sentiment": {"label": "neutral",
                      "scores": {"positive": 0, "neutral": 1, "negative": 0}}})
    monkeypatch.setattr(app_module, "synthesize", lambda text, voice: {
        "audio_base64": "AAAA", "mime_type": "audio/mp3", "voice": voice,
        "voice_name": "x", "char_count": len(text)})
    client = app_module.app.test_client()
    r = client.post("/process", data={"audio": (io.BytesIO(b"RIFF"), "t.wav")},
                    content_type="multipart/form-data")
    body = r.get_json()
    assert r.status_code == 200
    assert calls == ["en-US", "en-GB", "en-IN"]
    assert body["language"] == "en-IN" and body["stt_retry"]["improved"] is True
    assert [w["word"] for w in body["low_confidence_words"]] == ["jet", "tutor"]

    s = client.get("/stats").get_json()
    assert s["transcriptions"] == 1 and s["retry"]["retried"] == 1
    assert client.get("/stats?recent=x").status_code == 400
