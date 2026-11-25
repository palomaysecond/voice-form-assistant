import os

# Set before the app (and its load_dotenv) is imported: offline tests never
# send telemetry and always use the in-memory session store, never Cosmos DB.
os.environ["TELEMETRY_DISABLED"] = "1"
os.environ["COSMOS_ENDPOINT"] = ""
