"""OFX/QFX adapter (TD chequing + Visa file drop).

ofxtools 1.1.1 API (verified against installed source):
  OFXTree().parse(path) -> .convert() -> ofx.statements[*]
  statement: .account.acctid, .curdef, .ledgerbal.balamt/.dtasof, .transactions
  transaction: .dtposted (tz-aware datetime), .trnamt (Decimal), .fitid, .name, .memo

Dedup is by FITID, so description drift between exports never creates duplicates.
Malformed files are quarantined (banks emit bad SGML); unmapped ACCTIDs are a config
error and are surfaced, not silently dropped.

Date handling: OFX DTPOSTED is the bank's local *posting date* (time is filler). We
take its date component directly with NO timezone conversion — unlike WS/Plaid, whose
values are true UTC instants that DO get converted to America/Vancouver.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Union

from bankapp import money
from bankapp.config import AccountConfig
from bankapp.ingest.core import NormalizedTxn, make_txn


class MalformedOFXError(Exception):
    """Wraps any parse/convert failure from ofxtools so the caller can quarantine."""


class UnmappedAccountError(ValueError):
    """OFX ACCTID had no matching accounts.ofx_acctid in config."""


@dataclass
class OFXIngestResult:
    txns: list[NormalizedTxn] = field(default_factory=list)
    quarantined: bool = False
    quarantine_path: Optional[Path] = None


def acctid_map(accounts: Iterable[AccountConfig]) -> dict[str, str]:
    """Build {ofx_acctid: account_key} from configured accounts."""
    return {a.ofx_acctid: a.key for a in accounts if a.ofx_acctid}


def _parse(path: Union[str, Path]):
    from ofxtools.Parser import OFXTree

    try:
        tree = OFXTree()
        tree.parse(str(path))
        return tree.convert()
    except UnmappedAccountError:
        raise
    except Exception as exc:  # malformed SGML/header/etc. -> quarantine, never crash
        raise MalformedOFXError(f"failed to parse OFX {path}: {exc}") from exc


def ofx_to_txns(path: Union[str, Path], acctid_to_key: dict[str, str]) -> list[NormalizedTxn]:
    """Parse an OFX/QFX file into NormalizedTxn rows. Raises MalformedOFXError on parse
    failure and UnmappedAccountError on an unknown ACCTID."""
    ofx = _parse(path)
    out: list[NormalizedTxn] = []
    for st in ofx.statements:
        acctid = str(st.account.acctid)
        key = acctid_to_key.get(acctid)
        if key is None:
            raise UnmappedAccountError(
                f"OFX ACCTID '{acctid}' is not mapped — set ofx_acctid for the right "
                f"account in config [[accounts]]."
            )
        currency = getattr(st, "curdef", None) or "CAD"
        for tx in st.transactions:
            name = (tx.name or "").strip()
            memo = (getattr(tx, "memo", None) or "").strip()
            desc_raw = " ".join(p for p in (name, memo) if p) or "(no description)"
            out.append(
                make_txn(
                    account_key=key,
                    posted_date=tx.dtposted.strftime("%Y-%m-%d"),
                    amount_minor=money.to_minor(tx.trnamt, currency),
                    currency=currency,
                    description_raw=desc_raw,
                    dedup_key=f"fitid:{tx.fitid}",
                    source="ofx",
                )
            )
    return out


def ingest_ofx_file(
    path: Union[str, Path],
    acctid_to_key: dict[str, str],
    quarantine_dir: Optional[Union[str, Path]] = None,
) -> OFXIngestResult:
    """Parse one OFX file; on malformed content move it to quarantine_dir and return an
    empty result. UnmappedAccountError propagates (config error, not a bad file)."""
    path = Path(path)
    try:
        txns = ofx_to_txns(path, acctid_to_key)
        return OFXIngestResult(txns=txns)
    except MalformedOFXError:
        qpath = None
        if quarantine_dir is not None:
            qdir = Path(quarantine_dir)
            qdir.mkdir(parents=True, exist_ok=True)
            qpath = qdir / path.name
            shutil.move(str(path), str(qpath))
        return OFXIngestResult(txns=[], quarantined=True, quarantine_path=qpath)
