"""Part H option 2: confidence-based retry pipeline.

- Words whose Speech confidence is below WORD_LOW_CONFIDENCE are flagged so
  the page can highlight them.
- If the overall confidence is below RETRY_BELOW, the same audio is sent to
  Azure Speech again with other English locales (en-GB, then en-IN) and the result with
  the higher confidence is kept.

The Speech call is passed in, so this logic is unit-tested without Azure.
"""
import os
from contextlib import nullcontext

WORD_LOW_CONFIDENCE = 0.70
RETRY_BELOW = 0.85
PRIMARY_LANGUAGE = "en-US"
# en-GB is the assignment's example. In westus, en-GB/en-AU/en-CA/en-NZ gave
# byte-identical results to en-US for our recordings (same model), while
# en-IN is a different model; so both are tried, in this order.
# Override with STT_RETRY_LANGUAGES="en-GB,en-IN".
RETRY_LANGUAGES = tuple(
    l.strip() for l in os.environ.get("STT_RETRY_LANGUAGES", "en-GB,en-IN").split(",")
    if l.strip())


def low_confidence_words(words, threshold=WORD_LOW_CONFIDENCE):
    """Words with a returned confidence below the threshold, in spoken order."""
    return [{"word": w["word"], "confidence": w["confidence"],
             "offset": w.get("offset")}
            for w in words or []
            if w.get("confidence") is not None and w["confidence"] < threshold]


def transcribe_with_retry(transcribe_fn, audio_path, primary=PRIMARY_LANGUAGE,
                          retry_languages=RETRY_LANGUAGES, threshold=RETRY_BELOW,
                          retry_span=None):
    """Return the best transcription plus a `retry` record of what was tried.

    transcribe_fn(path, language=...) is speech_service.transcribe.
    retry_span: optional zero-argument callable returning a context manager
    (a tracing span) that wraps the retry calls.
    A failed retry never fails the request: the first result is kept.
    """
    first = transcribe_fn(audio_path, language=primary)
    attempts = [{"language": primary, "confidence": first["confidence"]}]
    best = first

    retried = first["confidence"] < threshold
    if retried:
        with (retry_span() if retry_span else nullcontext()) as span:
            for lang in retry_languages:
                try:
                    alt = transcribe_fn(audio_path, language=lang)
                except Exception as e:  # noqa: BLE001 — keep the first result
                    attempts.append({"language": lang, "error": str(e)[:200]})
                    continue
                attempts.append({"language": lang, "confidence": alt["confidence"]})
                if alt["transcript"] and alt["confidence"] > best["confidence"]:
                    best = alt
            if span is not None and hasattr(span, "set_attribute"):
                span.set_attribute("stt.retry.languages", ",".join(retry_languages))
                span.set_attribute("stt.retry.chosen_language", best["language"])
                span.set_attribute("stt.retry.improved", best is not first)

    result = dict(best)
    result["retry"] = {
        "threshold": threshold,
        "retried": retried,
        "attempts": attempts,
        "primary_confidence": first["confidence"],
        "chosen_language": best["language"],
        "improved": best is not first,
    }
    result["low_confidence_words"] = low_confidence_words(best["words"])
    return result
