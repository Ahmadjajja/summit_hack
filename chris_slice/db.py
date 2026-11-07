"""MongoDB access for TripCheck (database: tripcheck)."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

load_dotenv(override=True)

_URI = os.environ.get("MONGODB_URI")
if not _URI:
    raise RuntimeError("MONGODB_URI is missing. Copy .env.example to .env and fill it in.")

client: MongoClient = MongoClient(_URI)
db: Database = client["tripcheck"]

requests: Collection = db["requests"]
candidates: Collection = db["candidates"]
plans: Collection = db["plans"]
verdicts: Collection = db["verdicts"]
run_log: Collection = db["run_log"]
ledger: Collection = db["ledger"]
