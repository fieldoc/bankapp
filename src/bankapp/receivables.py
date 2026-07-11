"""Manual settlement of a receivable: a roommate pays their share back in cash (no
bank transaction to match), so it can't be matched by match_splits(). Settlement is
keyed on (template_id, period_key) -- NOT groups.id -- because match_splits() DELETEs
and rebuilds every split-expense group on every run (new group ids each time); the
template/period pair is the only handle stable across a rebuild. See v_receivables
in schema.sql for how settled_minor folds into outstanding_minor.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from bankapp.ingest.core import _utc_now_iso


class ReceivableNotFound(Exception):
    """Raised when a group id, or a (template name, period_key) pair, does not
    resolve to an existing split-expense receivable."""


def _upsert_settlement(
    conn: sqlite3.Connection,
    template_id: int,
    period_key: str,
    amount_minor: int,
    note: Optional[str],
    now: str,
) -> None:
    conn.execute(
        """INSERT INTO receivable_settlement(template_id, period_key, amount_minor, note, settled_at)
             VALUES (?,?,?,?,?)
           ON CONFLICT(template_id, period_key) DO UPDATE SET
             amount_minor = excluded.amount_minor,
             note = excluded.note,
             settled_at = excluded.settled_at""",
        (template_id, period_key, amount_minor, note, now),
    )


def _settle(
    conn: sqlite3.Connection,
    template_id: int,
    template_name: str,
    period_key: str,
    row: sqlite3.Row,
    amount_minor: Optional[int],
    note: Optional[str],
    now: Optional[str],
) -> dict:
    """Shared upsert + result-shaping for settle_group/settle_by_template, once the
    caller has resolved the (template_id, period_key, v_receivables row) triple."""
    now = now or _utc_now_iso()
    # Default = the full non-bank outstanding, ignoring any prior settlement, so
    # "mark settled" always fully clears regardless of how it was called before.
    default_minor = row["expected_minor"] - row["received_minor"]
    settled_minor = amount_minor if amount_minor is not None else default_minor

    with conn:
        _upsert_settlement(conn, template_id, period_key, settled_minor, note, now)

    outstanding = conn.execute(
        "SELECT outstanding_minor FROM v_receivables WHERE template = ? AND period_key = ?",
        (template_name, period_key),
    ).fetchone()["outstanding_minor"]

    return {
        "template": template_name,
        "period_key": period_key,
        "settled_minor": settled_minor,
        "outstanding_minor": outstanding,
    }


def settle_group(
    conn: sqlite3.Connection,
    group_id: int,
    amount_minor: Optional[int] = None,
    note: Optional[str] = None,
    now: Optional[str] = None,
) -> dict:
    """Settle the receivable behind a given (current) group id.

    Looks up the group's template_id/period_key/expected/received from
    v_receivables, then upserts by (template_id, period_key) -- so the settlement
    survives the next match_splits() rebuild even though this group_id will not.
    """
    row = conn.execute(
        """SELECT g.template_id, v.template, v.period_key, v.expected_minor, v.received_minor
           FROM v_receivables v JOIN groups g ON g.id = v.group_id
           WHERE v.group_id = ?""",
        (group_id,),
    ).fetchone()
    if row is None:
        raise ReceivableNotFound(f"no receivable group with id {group_id}")
    return _settle(
        conn, row["template_id"], row["template"], row["period_key"], row,
        amount_minor, note, now,
    )


def settle_by_template(
    conn: sqlite3.Connection,
    template_name: str,
    period_key: str,
    amount_minor: Optional[int] = None,
    note: Optional[str] = None,
    now: Optional[str] = None,
) -> dict:
    """Settle the receivable for a template name + period_key, independent of any
    current group id (useful once a rebuild has changed it)."""
    tmpl = conn.execute(
        "SELECT id FROM recurring_templates WHERE name = ?", (template_name,)
    ).fetchone()
    if tmpl is None:
        raise ReceivableNotFound(f"no template named {template_name!r}")
    template_id = tmpl["id"]

    row = conn.execute(
        "SELECT expected_minor, received_minor FROM v_receivables WHERE template = ? AND period_key = ?",
        (template_name, period_key),
    ).fetchone()
    if row is None:
        raise ReceivableNotFound(
            f"no receivable group for template {template_name!r} period {period_key!r}"
        )
    return _settle(conn, template_id, template_name, period_key, row, amount_minor, note, now)
