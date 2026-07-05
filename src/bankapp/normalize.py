"""Description normalization + dedup-key recipe.

Dedup-key preference per row: FITID (OFX/QFX) -> WS activity id -> Plaid txn id ->
content hash (TD CSV, which carries no ids). Only the content-hash path lives here;
id-based keys are trivial string prefixes built by the adapters.

HASH_VERSION and norm_desc are FROZEN once real data lands: changing either would
change every content hash and require a migration (out of scope). Richer merchant
normalization for *categorization* may evolve independently; it must never touch
the two frozen pieces below.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import Iterable, Sequence, TypeVar

HASH_VERSION = "1"


def norm_desc(raw: str) -> str:
    """Frozen normalization used INSIDE the content hash: lowercase + collapse whitespace."""
    return " ".join(raw.split()).lower()


def content_dedup_key(
    account_key: str,
    posted_date: str,
    amount_minor: int,
    currency: str,
    desc_norm: str,
    occurrence: int,
) -> str:
    """Content hash for id-less rows. Stable across overlapping whole-day export windows."""
    payload = "|".join(
        [
            HASH_VERSION,
            account_key,
            posted_date,
            str(amount_minor),
            currency,
            desc_norm,
            str(occurrence),
        ]
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def occurrences_for(keys: Sequence) -> list[int]:
    """Occurrence index (0,1,2...) for each key in FILE ORDER.

    Same-day-identical rows get 0,1,... so their content hashes differ. The counter
    is scoped to the file/batch, never the DB — DB-scoped counting would re-number on
    re-run and break idempotency.
    """
    seen: Counter = Counter()
    out: list[int] = []
    for k in keys:
        out.append(seen[k])
        seen[k] += 1
    return out


T = TypeVar("T")


def assign_occurrences(txns: Iterable[T]) -> list[T]:
    """Set ``.occurrence`` on each staging object from its (account_key, posted_date,
    amount_minor, desc_norm) key, in file order. Operates on MUTABLE staging objects,
    before the frozen NormalizedTxn is built."""
    txns = list(txns)
    keys = [(t.account_key, t.posted_date, t.amount_minor, t.desc_norm) for t in txns]
    for t, occ in zip(txns, occurrences_for(keys)):
        t.occurrence = occ
    return txns
