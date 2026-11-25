import os
import sys

# Make the app modules importable however pytest is started
# (CI runs plain `pytest`, which does not add the project root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set before the app (and its load_dotenv) is imported: offline tests never
# send telemetry and always use the in-memory session store, never Cosmos DB.
os.environ["TELEMETRY_DISABLED"] = "1"
os.environ["COSMOS_ENDPOINT"] = ""

# Part H: the SQLite confidence log goes to a throwaway file, never ./data/.
import tempfile  # noqa: E402
os.environ["STATS_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="vfa-tests-"), "stats.db")
