import shutil
from pathlib import Path

import pytest

from bankapp.config import AccountConfig
from bankapp.ingest import ofx

FIX = Path(__file__).resolve().parent / "fixtures"

ACCOUNTS = [
    AccountConfig(key="td-chequing", institution="td", type="chequing", currency="CAD", ofx_acctid="1111111"),
    AccountConfig(key="td-visa", institution="td", type="visa", currency="CAD", ofx_acctid="4519111122223333"),
]
ACCTID_TO_KEY = ofx.acctid_map(ACCOUNTS)


def test_acctid_map_ignores_unset():
    accts = ACCOUNTS + [AccountConfig(key="ws-cash", institution="wealthsimple", type="cash")]
    m = ofx.acctid_map(accts)
    assert m == {"1111111": "td-chequing", "4519111122223333": "td-visa"}


def test_chequing_parsed_with_fitid_keys():
    txns = ofx.ofx_to_txns(FIX / "td_chequing_jan.ofx", ACCTID_TO_KEY)
    assert len(txns) == 3
    assert all(t.account_key == "td-chequing" for t in txns)
    assert all(t.source == "ofx" for t in txns)
    keys = {t.dedup_key for t in txns}
    assert keys == {"fitid:CHQFIT001", "fitid:CHQFIT002", "fitid:CHQFIT003"}


def test_signed_amounts_and_dates():
    txns = ofx.ofx_to_txns(FIX / "td_chequing_jan.ofx", ACCTID_TO_KEY)
    by_fitid = {t.dedup_key: t for t in txns}
    assert by_fitid["fitid:CHQFIT001"].amount_minor == -1234  # debit negative
    assert by_fitid["fitid:CHQFIT002"].amount_minor == 250000  # credit positive
    assert by_fitid["fitid:CHQFIT001"].posted_date == "2026-01-15"
    # memo folded into raw description, normalized
    assert "shoppers drug mart" in by_fitid["fitid:CHQFIT001"].description_norm
    assert "purchase" in by_fitid["fitid:CHQFIT001"].description_norm


def test_visa_credit_card_statement():
    txns = ofx.ofx_to_txns(FIX / "td_visa_jan.qfx", ACCTID_TO_KEY)
    assert len(txns) == 3
    assert all(t.account_key == "td-visa" for t in txns)
    netflix = next(t for t in txns if t.dedup_key == "fitid:VISAFIT01")
    assert netflix.amount_minor == -4567
    assert netflix.currency == "CAD"


def test_unmapped_acctid_errors_with_id():
    with pytest.raises(ofx.UnmappedAccountError, match="1111111"):
        ofx.ofx_to_txns(FIX / "td_chequing_jan.ofx", {})  # empty mapping


def test_malformed_raises_without_quarantine_dir(tmp_path):
    staged = tmp_path / "malformed.ofx"
    shutil.copy(FIX / "malformed.ofx", staged)
    with pytest.raises(ofx.MalformedOFXError):
        ofx.ofx_to_txns(staged, ACCTID_TO_KEY)


def test_malformed_quarantined_no_rows(tmp_path):
    staged = tmp_path / "inbox" / "malformed.ofx"
    staged.parent.mkdir()
    shutil.copy(FIX / "malformed.ofx", staged)
    qdir = tmp_path / "quarantine"

    result = ofx.ingest_ofx_file(staged, ACCTID_TO_KEY, quarantine_dir=qdir)

    assert result.quarantined is True
    assert result.txns == []
    assert not staged.exists()                       # moved out of inbox
    assert (qdir / "malformed.ofx").exists()          # landed in quarantine


def test_good_file_not_quarantined(tmp_path):
    result = ofx.ingest_ofx_file(FIX / "td_visa_jan.qfx", ACCTID_TO_KEY, quarantine_dir=tmp_path / "q")
    assert result.quarantined is False
    assert len(result.txns) == 3
