"""Part D — Text-to-Speech read-back with Azure Neural TTS.

D1: build_summary() turns the form result + Language analysis into the text
    the user hears (confirmation first, then a short analysis summary).
D2: synthesize() returns MP3 (Audio16Khz32KBitRateMonoMp3) as Base64 so the
    browser can play it with <audio src="data:audio/mp3;base64,...">.
D3: three voices with different speaking styles (CSC 645 requirement).
"""
import base64
import os
from xml.sax.saxutils import escape

import azure.cognitiveservices.speech as speechsdk

from speech_service import SpeechServiceError

# key -> (label, Neural voice, SSML speaking style). Styles come from the
# Azure voice gallery; a voice ignores a style it does not support.
VOICES = {
    "formal": ("Formal (Guy, newscast)", "en-US-GuyNeural", "newscast"),
    "casual": ("Casual (Jenny, chat)", "en-US-JennyNeural", "chat"),
    "lively": ("Lively (Aria, cheerful)", "en-US-AriaNeural", "cheerful"),
}
DEFAULT_VOICE = "casual"

_SENTIMENT_WORDS = {"positive": "positive", "neutral": "neutral",
                    "negative": "negative", "mixed": "mixed"}


def _plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _join(items):
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def build_analysis_summary(lang):
    """D1: key phrase count + topics, sentiment, entity-type overview."""
    phrases = lang.get("key_phrases", [])
    if phrases:
        topics = _join([f'"{p}"' for p in phrases[:3]])
        s = f"I found {_plural(len(phrases), 'key phrase')}, including {topics}."
    else:
        s = "I found no key phrases."
    label = lang.get("sentiment", {}).get("label", "neutral")
    s += f" The overall tone was {_SENTIMENT_WORDS.get(label, label)}."
    counts = {}
    for e in lang.get("entities", []):
        cat = "date" if e["category"] == "DateTime" else e["category"].lower()
        counts[cat] = counts.get(cat, 0) + 1
    if counts:
        s += " I detected " + _join([_plural(n, c) for c, n in counts.items()]) + "."
    else:
        s += " I detected no named entities."
    return s


def build_summary(form, lang):
    """Spoken text: the form read-back (the product) + the analysis summary."""
    return form["confirmation_text"] + " " + build_analysis_summary(lang)


def resolve_voice(key):
    key = (key or DEFAULT_VOICE).strip().lower()
    if key not in VOICES:
        raise ValueError(f"Unknown voice '{key}'. Use one of: {', '.join(VOICES)}.")
    return key


def build_ssml(text, voice_key):
    _, voice, style = VOICES[voice_key]
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="en-US">'
        f'<voice name="{voice}"><mstts:express-as style="{style}">'
        f"{escape(text)}</mstts:express-as></voice></speak>")


def synthesize(text, voice_key=DEFAULT_VOICE):
    """Return {"audio_base64", "mime_type", "voice", "char_count"}."""
    voice_key = resolve_voice(voice_key)
    speech_config = speechsdk.SpeechConfig(
        subscription=os.environ["AZURE_SPEECH_KEY"],
        region=os.environ["AZURE_SPEECH_REGION"])
    speech_config.set_speech_synthesis_output_format(
        speechsdk.SpeechSynthesisOutputFormat.Audio16Khz32KBitRateMonoMp3)
    # audio_config=None: keep the audio in memory instead of playing it.
    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=speech_config, audio_config=None)
    result = synthesizer.speak_ssml_async(build_ssml(text, voice_key)).get()

    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        d = result.cancellation_details
        raise SpeechServiceError(
            f"Azure TTS error ({d.error_code}): {d.error_details}")
    return {
        "audio_base64": base64.b64encode(result.audio_data).decode("ascii"),
        "mime_type": "audio/mp3",
        "voice": voice_key,
        "voice_name": VOICES[voice_key][1],
        "char_count": len(text),
    }
