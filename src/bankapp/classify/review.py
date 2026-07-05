"""Review queue: transactions the rules couldn't classify.

The queue is DERIVED (no extra state): a txn is in the queue when it has no category
and no role_hint. The Claude categorize skill exports this, decides categories, and
writes verdicts back as RULES via `finance rules add` — then `finance categorize`
drains the queue. Never writes the DB here; this module only reads.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass

from bankapp import money


@dataclass(frozen=True)
class ReviewItem:
    raw_txn_id: int
    account_key: str
    posted_date: str
    amount_minor: int
    currency: str
    description_norm: str
    description_raw: str


_QUEUE_SQL = """
SELECT r.id AS raw_txn_id, a.key AS account_key, r.posted_date, r.amount_minor,
       r.currency, r.description_norm, r.description_raw
FROM raw_txn r
JOIN accounts a ON a.id = r.account_id
LEFT JOIN txn_interp i ON i.raw_txn_id = r.id
WHERE (i.raw_txn_id IS NULL) OR (i.category IS NULL AND i.role_hint IS NULL)
ORDER BY r.posted_date, r.id
"""


def queue(conn: sqlite3.Connection) -> list[ReviewItem]:
    return [
        ReviewItem(
            raw_txn_id=row["raw_txn_id"],
            account_key=row["account_key"],
            posted_date=row["posted_date"],
            amount_minor=row["amount_minor"],
            currency=row["currency"],
            description_norm=row["description_norm"],
            description_raw=row["description_raw"],
        )
        for row in conn.execute(_QUEUE_SQL)
    ]


def count(conn: sqlite3.Connection) -> int:
    return len(queue(conn))


def export_json(conn: sqlite3.Connection) -> str:
    return json.dumps([asdict(i) for i in queue(conn)], indent=2)


def export_markdown(conn: sqlite3.Connection) -> str:
    items = queue(conn)
    if not items:
        return "# Review queue\n\n(empty)\n"
    lines = [
        "# Review queue",
        "",
        f"{len(items)} uncategorized transaction(s).",
        "",
        "| id | account | date | amount | description |",
        "|----|---------|------|-------:|-------------|",
    ]
    for i in items:
        amount = f"{money.from_minor(i.amount_minor, i.currency)} {i.currency}"
        lines.append(
            f"| {i.raw_txn_id} | {i.account_key} | {i.posted_date} | {amount} | {i.description_norm} |"
        )
    return "\n".join(lines) + "\n"
