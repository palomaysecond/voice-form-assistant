"""Track 1 — multi-turn session state in Azure Cosmos DB (free tier).

One document per form session, partition key /id. The container's default
time-to-live is 7 days, so abandoned sessions delete themselves.

Without COSMOS_ENDPOINT (offline tests, quick local runs) an in-memory store
is used, so the rest of the app does not care where sessions live.
"""
import copy
import os
import threading

SESSION_TTL_SECONDS = 7 * 24 * 3600


class SessionStoreError(Exception):
    """Cosmos DB call failed -> HTTP 502."""


class InMemorySessionStore:
    kind = "memory"

    def __init__(self):
        self._docs = {}
        self._lock = threading.Lock()

    def get(self, session_id):
        with self._lock:
            doc = self._docs.get(session_id)
            return copy.deepcopy(doc) if doc else None

    def save(self, doc):
        with self._lock:
            self._docs[doc["id"]] = copy.deepcopy(doc)
        return doc


class CosmosSessionStore:
    kind = "cosmos"

    def __init__(self, endpoint, key, database, container):
        from azure.cosmos import CosmosClient
        client = CosmosClient(endpoint, credential=key)
        self._container = (client.get_database_client(database)
                           .get_container_client(container))

    def get(self, session_id):
        from azure.cosmos.exceptions import (CosmosHttpResponseError,
                                             CosmosResourceNotFoundError)
        try:
            doc = self._container.read_item(item=session_id,
                                            partition_key=session_id)
        except CosmosResourceNotFoundError:
            return None  # never existed, or the TTL already removed it
        except CosmosHttpResponseError as e:
            raise SessionStoreError(f"Cosmos DB error: {e.message}") from e
        return {k: v for k, v in doc.items() if not k.startswith("_")}

    def save(self, doc):
        from azure.cosmos.exceptions import CosmosHttpResponseError
        try:
            self._container.upsert_item(doc)
        except CosmosHttpResponseError as e:
            raise SessionStoreError(f"Cosmos DB error: {e.message}") from e
        return doc


_store = None


def get_store():
    global _store
    if _store is None:
        endpoint = os.environ.get("COSMOS_ENDPOINT")
        if endpoint:
            _store = CosmosSessionStore(
                endpoint, os.environ["COSMOS_KEY"],
                os.environ.get("COSMOS_DATABASE", "voiceform"),
                os.environ.get("COSMOS_CONTAINER", "sessions"))
        else:
            _store = InMemorySessionStore()
    return _store


def set_store(store):
    """Tests swap in a fresh InMemorySessionStore."""
    global _store
    _store = store
