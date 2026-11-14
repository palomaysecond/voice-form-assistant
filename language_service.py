"""Part C — Azure AI Language: NER, key phrases, sentiment.

Results are converted to plain dicts so the form logic (form_extractor.py)
does not depend on the SDK and can be unit-tested offline.
"""
import os

from azure.ai.textanalytics import TextAnalyticsClient
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import AzureError

_client = None


class LanguageServiceError(Exception):
    """Azure Language call failed -> HTTP 502."""


def _get_client():
    global _client
    if _client is None:
        _client = TextAnalyticsClient(
            endpoint=os.environ["AZURE_LANGUAGE_ENDPOINT"],
            credential=AzureKeyCredential(os.environ["AZURE_LANGUAGE_KEY"]))
    return _client


def analyze(text, language="en"):
    """Run NER, key phrase extraction and sentiment on one document."""
    client = _get_client()
    docs = [{"id": "1", "language": language, "text": text}]
    try:
        ner = client.recognize_entities(docs)[0]
        kp = client.extract_key_phrases(docs)[0]
        sent = client.analyze_sentiment(docs)[0]
    except AzureError as e:
        raise LanguageServiceError(f"Azure Language error: {e}") from e

    for r in (ner, kp, sent):
        if r.is_error:
            raise LanguageServiceError(f"Azure Language error: {r.error.message}")

    return {
        "entities": [
            {"text": e.text, "category": e.category,
             "subcategory": e.subcategory,
             "confidence": round(e.confidence_score, 3)}
            for e in ner.entities
        ],
        "key_phrases": list(kp.key_phrases),
        "sentiment": {
            "label": sent.sentiment,
            "scores": {
                "positive": sent.confidence_scores.positive,
                "neutral": sent.confidence_scores.neutral,
                "negative": sent.confidence_scores.negative,
            },
        },
    }
