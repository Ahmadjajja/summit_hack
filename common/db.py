"""MongoDB access. Every step reads its input here and writes its output here.

Falls back to an in-memory mongomock database when MONGODB_URI is empty,
so the whole pipeline runs on a laptop with no setup.
"""
from functools import lru_cache

from common import config


@lru_cache(maxsize=1)
def get_db():
    if config.MONGODB_URI:
        from pymongo import MongoClient
        return MongoClient(config.MONGODB_URI)[config.MONGODB_DB]
    import mongomock
    return mongomock.MongoClient()[config.MONGODB_DB]


def requests_col():
    return get_db()["requests"]


def candidates_col():
    return get_db()["candidates"]


def plans_col():
    return get_db()["plans"]


def verdicts_col():
    return get_db()["verdicts"]


def run_log_col():
    return get_db()["run_log"]


def ledger_col():
    return get_db()["ledger"]


def clean(doc):
    """Drop Mongo's _id so a document can be loaded into a pydantic model."""
    if doc is None:
        return None
    doc = dict(doc)
    doc.pop("_id", None)
    return doc
