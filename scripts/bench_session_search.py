"""Standalone session-search benchmark; deliberately excluded from CI.

Use an EMPTY disposable Postgres database, never an application database::

    createdb session_search_bench
    OMNIGENT_SKIP_WEB_UI=true uv sync --extra postgres --group dev
    uv run --no-sync python scripts/bench_session_search.py \
        --database-uri postgresql+psycopg:///session_search_bench
    dropdb session_search_bench

The default fixture has 1,000,000 items, 8,000 sessions, about 2.7 GB of
search text, and 20% of items concentrated in 1% of sessions. Text widths
range from 384 bytes to 1 MiB with deterministic, poorly compressible payloads.
Narrow ACLs grant 1% of sessions; broad ACLs grant 90%. Common, medium, rare,
absent, short and non-ASCII queries time the real list path (including snippets)
and the snippet lookup separately. Output is JSON lines with p50/p95 in ms,
sample counts and errors (including the unchanged 15-second search timeout).

Use --items 10000 --sessions 100 --repeats 3 for a smoke run, or --reuse to
measure an existing fixture again. Percentiles use nearest ranks; compare
repeated runs on the same idle machine and database. This measures warm runs,
not cold-cache or production tail latency. The script refuses a nonempty
database unless --reuse is explicit and never drops existing data.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import string
import time
from collections.abc import Callable

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
    _fetch_search_snippets,
)

QUERIES = {
    "common": "commonmarker",
    "medium": "mediummarker",
    "rare": "raremarker",
    "absent": "nevermatchsessionsearch",
    "short": "xy",
    "non_ascii": "\u65e5\u672c\u8a9e",
}


def seed(store: SqlAlchemyConversationStore, items: int, sessions: int) -> None:
    """Bulk-load synthetic rows while preserving the production schema and indexes."""
    rng = random.Random(8291)
    payload = "".join(rng.choices(string.ascii_lowercase + string.digits + " ", k=2**21))
    hot_sessions = max(1, sessions // 100)
    with store._conv_session("benchmark_seed_sessions") as session:
        session.execute(
            text("""
                INSERT INTO conversations
                    (workspace_id, id, created_at, updated_at, title,
                     root_conversation_id, next_position, archived)
                SELECT 0, decode(md5('session-' || s), 'hex'), s, s,
                    'benchmark ' || s, decode(md5('session-' || s), 'hex'), :items, false
                FROM generate_series(1, :sessions) s
            """),
            {"items": items, "sessions": sessions},
        )
        for name, threshold in [
            ("narrow", max(1, sessions // 100)),
            ("broad", sessions * 9 // 10),
        ]:
            session.execute(
                text("""
                    INSERT INTO session_permissions (workspace_id, user_id, conversation_id, level)
                    SELECT 0, :reader, decode(md5('session-' || s), 'hex'), 1
                    FROM generate_series(1, :threshold) s
                """),
                {"reader": name, "threshold": threshold},
            )
    started = time.monotonic()
    for start in range(1, items + 1, 10000):
        with store._conv_session("benchmark_seed_items") as session:
            session.execute(
                text("""
                    INSERT INTO conversation_items
                        (workspace_id, conversation_id, id, created_at,
                         response_id, status, position, type, data, search_text)
                    SELECT 0, decode(md5('session-' || (
                        CASE WHEN g % 5 = 0 THEN 1 + (g / 5) % :hot
                             ELSE 1 + :hot + (g - g / 5) % (:sessions - :hot) END
                        )), 'hex'), decode(md5('item-' || g), 'hex'), g,
                        'benchmark', 1, g, 1, '{}',
                        CASE WHEN g % 7 != 0 THEN 'commonmarker ' ELSE '' END ||
                        CASE WHEN g % 80 = 0 THEN 'mediummarker ' ELSE '' END ||
                        CASE WHEN g % 100000 < 2 THEN 'raremarker ' ELSE '' END ||
                        CASE WHEN g % 100 = 0 THEN :unicode ELSE '' END ||
                        convert_from(substring(:payload FROM 1 + (g % 1000) * 997 FOR
                            CASE WHEN g % 1000 = 0 THEN 1048576
                                 WHEN g % 100 < 1 THEN 65536
                                 WHEN g % 10 < 1 THEN 8192 ELSE 384 END), 'UTF8')
                    FROM generate_series(CAST(:start AS integer), CAST(:stop AS integer)) g
                """),
                {
                    "start": start,
                    "stop": min(items, start + 9999),
                    "sessions": sessions,
                    "hot": hot_sessions,
                    "payload": payload.encode("ascii"),
                    "unicode": QUERIES["non_ascii"] + " ",
                },
            )
        if start == 1 or (start - 1) % 100000 == 0:
            print(
                json.dumps(
                    {
                        "seeded_items": min(items, start + 9999),
                        "seconds": round(time.monotonic() - started, 2),
                    }
                ),
                flush=True,
            )
    with store._conv_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        connection.execute(text("ANALYZE conversations"))
        connection.execute(text("ANALYZE conversation_items"))
        connection.execute(text("ANALYZE session_permissions"))


def measure(run: Callable[[], object], repeats: int) -> dict[str, object]:
    samples = []
    errors: dict[str, int] = {}
    for _ in range(repeats):
        start = time.monotonic()
        try:
            run()
        except DBAPIError as exc:
            code = str(getattr(exc.orig, "sqlstate", type(exc.orig).__name__))
            errors[code] = errors.get(code, 0) + 1
        samples.append((time.monotonic() - start) * 1000)
    samples.sort()
    return {
        "samples": repeats,
        "p50_ms": round(samples[math.ceil(repeats * 0.5) - 1], 2),
        "p95_ms": round(samples[math.ceil(repeats * 0.95) - 1], 2),
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--database-uri", required=True)
    parser.add_argument("--items", type=int, default=1000000)
    parser.add_argument("--sessions", type=int, default=8000)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--reuse", action="store_true")
    args = parser.parse_args()
    if args.sessions < 2 or args.items < args.sessions or args.repeats < 1:
        parser.error("Require items >= sessions >= 2 and repeats >= 1")
    engine = create_engine(args.database_uri)
    try:
        if engine.dialect.name != "postgresql":
            parser.error("This benchmark requires Postgres")
        if inspect(engine).get_table_names() and not args.reuse:
            parser.error("Database is not empty; use a disposable database or --reuse")
    finally:
        engine.dispose()
    store = SqlAlchemyConversationStore(args.database_uri)
    if not args.reuse:
        seed(store, args.items, args.sessions)
    with store._conv_session("benchmark_fixture_stats") as session:
        stats = session.execute(
            text("SELECT count(*), sum(octet_length(search_text)) FROM conversation_items")
        ).one()
        version = session.execute(text("SHOW server_version")).scalar_one()
    print(
        json.dumps({"items": stats[0], "text_bytes": int(stats[1] or 0), "postgres": version}),
        flush=True,
    )
    for acl in ("narrow", "broad"):
        page = store.list_conversations(accessible_by=acl, limit=20)
        ids = [conv.id for conv in page.data]
        for category, query in QUERIES.items():

            def listing(query=query, acl=acl):
                return store.list_conversations(search_query=query, accessible_by=acl, limit=20)

            def snippets(ids=ids, query=query):
                with store._conv_session("benchmark_search_snippets") as session:
                    session.execute(text("SET LOCAL statement_timeout = 15000"))
                    return _fetch_search_snippets(session, ids, query)

            for path, run in [("list_with_snippets", listing), ("snippets", snippets)]:
                print(
                    json.dumps(
                        {"acl": acl, "term": category, "path": path, **measure(run, args.repeats)}
                    ),
                    flush=True,
                )


if __name__ == "__main__":
    main()
