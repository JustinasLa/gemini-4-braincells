"""Apply the DB schema: db/schema.sql, then db/migrations/*.sql in name order.

    python -m common.migrate              # uses DATABASE_URL
    python -m common.migrate --url postgresql://...

Every file is idempotent, so re-running is safe. Applied migration names are recorded in
`schema_migrations` and skipped next time; db/schema.sql (CREATE ... IF NOT EXISTS) always runs.
"""

import argparse
import sys
from pathlib import Path

import psycopg

from common.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "db" / "schema.sql"
MIGRATIONS = ROOT / "db" / "migrations"


def migrate(url: str) -> list[str]:
    applied_now: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(SCHEMA.read_text())
        done: set[str] = set()
        if conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0]:
            done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute(
                    "INSERT INTO schema_migrations (name) VALUES (%s) ON CONFLICT DO NOTHING",
                    (path.name,),
                )
            applied_now.append(path.name)
    return applied_now


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=None, help="Postgres URL (default: DATABASE_URL)")
    args = parser.parse_args()
    applied = migrate(args.url or get_settings().database_url)
    print("applied:", ", ".join(applied) if applied else "nothing new", file=sys.stderr)


if __name__ == "__main__":
    main()
