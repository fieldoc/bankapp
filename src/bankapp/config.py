"""TOML config: load, validate, apply env overrides, expand ~.

Resolution order (documented in config.example.toml):
  config: $FINANCE_CONFIG > %APPDATA%\\bankapp\\config.toml (Windows)
                          > ~/.config/bankapp/config.toml (else)
  db:     $FINANCE_DB > config db_path

All paths are ``~``-expanded via pathlib so one config works on Windows and macOS.
Money strings are converted to integer minor units here, at the boundary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from bankapp import money

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Windows deploy on <3.11
    import tomli as tomllib  # type: ignore


@dataclass(frozen=True)
class AccountConfig:
    key: str
    institution: str
    type: str
    currency: str = "CAD"
    ofx_acctid: Optional[str] = None


@dataclass(frozen=True)
class PlaidConfig:
    enabled: bool = False
    environment: str = "production"


@dataclass(frozen=True)
class GoalConfig:
    name: str
    target_minor: int
    currency: str
    start_date: str
    target_date: Optional[str]
    allocation_pct: int
    note: Optional[str] = None


@dataclass(frozen=True)
class TemplateConfig:
    name: str
    kind: str
    expected_amount_minor: int
    currency: str
    share_numer: int
    share_denom: int
    day_of_month: int
    expense_account: str
    expense_pattern: str
    reimburse_account: str
    reimburser_pattern: str
    amount_tolerance_minor: int
    window_days: int
    link_transfer: bool
    cadence: str = "monthly"
    # Minimum inflow amount to count as a reimbursement. Needed when the bank
    # anonymizes e-transfer senders (TD via Plaid: 'e-transfer ***kgt'), making the
    # pattern too broad — the amount becomes the discriminating signal. 0 = no gate.
    reimburse_min_minor: int = 0


@dataclass(frozen=True)
class TransfersConfig:
    window_days: int = 7
    tolerance_minor: int = 0
    seed_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    timezone: str
    db_path: Path
    ingest_dir: Path
    accounts: tuple[AccountConfig, ...]
    plaid: PlaidConfig
    budgets: dict[str, int]
    goals: tuple[GoalConfig, ...]
    templates: tuple[TemplateConfig, ...]
    leak_threshold_minor: int
    transfers: TransfersConfig
    config_path: Path


def resolve_config_path() -> Path:
    """Platform-appropriate default config path (before env override)."""
    env = os.environ.get("FINANCE_CONFIG")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":  # pragma: no cover - exercised on Windows deploy
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / "bankapp" / "config.toml"
    return Path.home() / ".config" / "bankapp" / "config.toml"


def _parse_share(s: str) -> tuple[int, int]:
    numer, _, denom = s.partition("/")
    return int(numer.strip()), int(denom.strip())


def _expand(p: str) -> Path:
    return Path(p).expanduser()


def load_config(path: Optional[Path] = None) -> Config:
    """Load and validate config from ``path`` (or the resolved default)."""
    cfg_path = Path(path).expanduser() if path is not None else resolve_config_path()
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"Config not found at {cfg_path}. "
            f"Copy config.example.toml to that location and edit it, "
            f"or set $FINANCE_CONFIG to point at your config file."
        )
    with cfg_path.open("rb") as fh:
        data = tomllib.load(fh)

    # DB path: env override wins over config db_path.
    db_env = os.environ.get("FINANCE_DB")
    if db_env:
        db_path = Path(db_env).expanduser()
    else:
        db_path = _expand(data["db_path"])

    accounts = tuple(
        AccountConfig(
            key=a["key"],
            institution=a["institution"],
            type=a["type"],
            currency=a.get("currency", "CAD"),
            ofx_acctid=(a.get("ofx_acctid") or None),
        )
        for a in data.get("accounts", [])
    )

    plaid_raw = data.get("plaid", {})
    plaid = PlaidConfig(
        enabled=bool(plaid_raw.get("enabled", False)),
        environment=plaid_raw.get("environment", "production"),
    )

    budgets = {
        cat: money.to_minor(val, "CAD")
        for cat, val in data.get("budgets", {}).items()
    }

    goals = tuple(
        GoalConfig(
            name=g["name"],
            target_minor=money.to_minor(g["target"], g.get("currency", "CAD")),
            currency=g.get("currency", "CAD"),
            start_date=g["start_date"],
            target_date=g.get("target_date"),
            allocation_pct=int(g.get("allocation_pct", 100)),
            note=g.get("note"),
        )
        for g in data.get("goals", [])
    )

    templates = tuple(
        _parse_template(t) for t in data.get("templates", [])
    )

    advisor = data.get("advisor", {})
    leak_threshold_minor = money.to_minor(advisor.get("leak_threshold", "15.00"), "CAD")

    tr = data.get("transfers", {})
    transfers = TransfersConfig(
        window_days=int(tr.get("window_days", 7)),
        tolerance_minor=money.to_minor(tr.get("tolerance", "0.00"), "CAD"),
        seed_patterns=tuple(p.lower() for p in tr.get("seed_patterns", [])),
    )

    return Config(
        timezone=data.get("timezone", "America/Vancouver"),
        db_path=db_path,
        ingest_dir=_expand(data.get("ingest_dir", "~/finance/inbox")),
        accounts=accounts,
        plaid=plaid,
        budgets=budgets,
        goals=goals,
        templates=templates,
        leak_threshold_minor=leak_threshold_minor,
        transfers=transfers,
        config_path=cfg_path,
    )


def _parse_template(t: dict) -> TemplateConfig:
    currency = t.get("currency", "CAD")
    numer, denom = _parse_share(t["share"])
    return TemplateConfig(
        name=t["name"],
        kind=t["kind"],
        expected_amount_minor=money.to_minor(t["expected_amount"], currency),
        currency=currency,
        share_numer=numer,
        share_denom=denom,
        day_of_month=int(t.get("day_of_month", 1)),
        expense_account=t["expense_account"],
        expense_pattern=t["expense_pattern"],
        reimburse_account=t["reimburse_account"],
        reimburser_pattern=t["reimburser_pattern"],
        amount_tolerance_minor=money.to_minor(t.get("amount_tolerance", "5.00"), currency),
        window_days=int(t.get("window_days", 45)),
        link_transfer=bool(t.get("link_transfer", True)),
        cadence=t.get("cadence", "monthly"),
        reimburse_min_minor=money.to_minor(t.get("reimburse_min_amount", "0.00"), currency),
    )
