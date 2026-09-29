#!/usr/bin/env python3
"""Check history reads during a write with the deployed schema and SQLite URI."""

from pathlib import Path
import sqlite3
import tempfile

schema = Path(__file__).resolve().parents[1] / "collector/history_schema.sql"
with tempfile.TemporaryDirectory(prefix="freematics-history-concurrency-") as directory:
    path = Path(directory) / "history.sqlite"
    writer = sqlite3.connect(path, timeout=0)
    writer.executescript(schema.read_text())
    writer.execute("CREATE TABLE concurrency_probe(value INTEGER)")
    writer.execute("INSERT INTO concurrency_probe VALUES (1)")
    writer.commit()
    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0)
    reader.execute("BEGIN")
    assert reader.execute("SELECT value FROM concurrency_probe").fetchone() == (1,)
    try:
        writer.execute("UPDATE concurrency_probe SET value=2")
        writer.commit()
        assert reader.execute("SELECT value FROM concurrency_probe").fetchone() == (1,)
        fresh = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0)
        assert fresh.execute("SELECT value FROM concurrency_probe").fetchone() == (2,)
        fresh.close()
    finally:
        reader.close()
        writer.close()
    # A reader can recreate SQLite sidecars after the final writer exits.
    fresh = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0)
    assert fresh.execute("SELECT value FROM concurrency_probe").fetchone() == (2,)
    try:
        fresh.execute("UPDATE concurrency_probe SET value=3")
    except sqlite3.OperationalError as error:
        assert "readonly" in str(error)
    else:
        raise AssertionError("History reader allowed database writes")
    fresh.close()
print("PASS: concurrent commit, stable reader, fresh data, reopen and read-only queries")
