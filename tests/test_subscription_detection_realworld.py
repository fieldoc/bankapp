"""Regression: the subscription/leak engine vs real-world billing behavior.

Three confirmed live failures drove these tests:
1. merchant_token() took the first whitespace token, so every Wealthsimple line
   ("purchase: X", "deposit: Y", "pre-authorized debit: to Z") collapsed into one
   meaningless 'purchase:' bucket — the leak report was unreadable.
2. Amount stability (every charge within ±5% of the overall median) was a hard GATE,
   so any subscription whose price ever moved (trial -> paid -> plan change) was
   deleted from the report entirely — the user's largest recurring charge was
   invisible. Stability is now judged on the dominant amount-cluster instead.
3. price_creep fired on any latest > trailing-median, i.e. a 1.5-cent FX wobble.
   Creep now requires a material move (>1% AND >= 25 minor units).
"""

from bankapp.report import advisor


# ---- merchant_token: strip transaction-type prefixes -------------------------

def test_token_strips_ws_purchase_prefix():
    assert advisor.merchant_token("purchase: koodo mobile victoria") == "koodo"


def test_token_strips_multiword_prefix_and_to():
    assert advisor.merchant_token("pre-authorized debit: to intuit canada u") == "intuit"


def test_token_strips_from_after_prefix():
    assert advisor.merchant_token("direct deposit: from cloud produce a") == "cloud"


def test_token_plain_descriptions_unchanged():
    assert advisor.merchant_token("netflix.com 866-579-7172 on") == "netflix.com"
    assert advisor.merchant_token("monthly account fee") == "monthly"
    assert advisor.merchant_token("") == "(unknown)"


def test_leaks_group_by_real_merchant_not_prefix():
    txns = [
        ("2026-01-03", -400, "purchase: tim hortons #4021", None, "CAD"),
        ("2026-01-10", -350, "purchase: tim hortons #77", None, "CAD"),
        ("2026-01-12", -500, "purchase: koodo mobile", None, "CAD"),
    ]
    by_merchant = {r.merchant: r for r in advisor.leak_report(txns, threshold_minor=1500)}
    assert by_merchant["tim"].count == 2
    assert by_merchant["koodo"].count == 1
    assert "purchase:" not in by_merchant


# ---- detector: a price change must not delete the subscription ---------------

def _t(d, amt, desc="acme.ai", cur="CAD"):
    return (d, amt, desc, cur)


def test_price_change_does_not_hide_subscription():
    """Trial price -> full price: the old ±5% whole-range gate dropped this merchant
    entirely. The dominant cluster (3x -15680) must keep it visible, priced at the
    current amount."""
    txns = [
        _t("2026-03-02", -3136),    # intro/trial
        _t("2026-03-12", -13595),   # first real bill
        _t("2026-04-13", -15680),
        _t("2026-05-13", -15680),
        _t("2026-06-08", -15680),
    ]
    subs = advisor.detect_subscriptions(txns)
    assert len(subs) == 1
    assert subs[0].monthly_cost_minor == 15680  # current price, not the lifetime median
    assert subs[0].count == 5


def test_one_spike_month_does_not_hide_subscription():
    """A single anomalous bill (usage overage) must not delete the subscription."""
    txns = [
        _t("2026-03-13", -15680), _t("2026-04-13", -15680), _t("2026-05-13", -15680),
        _t("2026-06-08", -27882),   # spike month
        _t("2026-07-06", -15680),
    ]
    subs = advisor.detect_subscriptions(txns)
    assert len(subs) == 1
    assert subs[0].monthly_cost_minor == 15680


def test_erratic_amounts_still_excluded():
    """No dominant amount-cluster = not a subscription (regular-cadence groceries
    must not flood the report)."""
    txns = [
        _t("2026-03-05", -4210, "purchase: save-on foods"),
        _t("2026-04-03", -11890, "purchase: save-on foods"),
        _t("2026-05-06", -6733, "purchase: save-on foods"),
        _t("2026-06-04", -2410, "purchase: save-on foods"),
    ]
    assert advisor.detect_subscriptions(txns) == []


# ---- price creep needs a material move ---------------------------------------

def test_fx_cent_wobble_is_not_price_creep():
    txns = [_t("2025-12-27", -3144), _t("2026-01-27", -3151), _t("2026-02-27", -3149)]
    subs = advisor.detect_subscriptions(txns)
    assert len(subs) == 1
    assert subs[0].price_creep is False   # 1.5 cents over trailing median = noise


def test_real_price_jump_is_creep():
    txns = [
        _t("2026-01-03", -1599), _t("2026-02-03", -1599), _t("2026-03-03", -1599),
        _t("2026-04-03", -1799),  # $2 plan increase
    ]
    subs = advisor.detect_subscriptions(txns)
    assert len(subs) == 1
    assert subs[0].price_creep is True


# ---- counterparty merges vendor renames ---------------------------------------

def test_counterparty_merges_vendor_rename():
    """Same vendor billing under two names: a rule-assigned counterparty groups
    them into one subscription."""
    txns = [
        ("2026-03-13", -15680, "claude.ai", "CAD", "anthropic"),
        ("2026-04-13", -15680, "claude.ai", "CAD", "anthropic"),
        ("2026-05-13", -15680, "claude.ai", "CAD", "anthropic"),
        ("2026-06-11", -15680, "anthropic", "CAD", "anthropic"),
        ("2026-07-09", -15680, "anthropic", "CAD", "anthropic"),
    ]
    subs = advisor.detect_subscriptions(txns)
    assert len(subs) == 1
    assert subs[0].merchant == "anthropic"
    assert subs[0].count == 5
    assert subs[0].monthly_cost_minor == 15680


def test_counterparty_absent_falls_back_to_token():
    txns = [
        ("2026-01-03", -1599, "netflix.com monthly", "CAD"),
        ("2026-02-02", -1599, "netflix.com monthly", "CAD"),
        ("2026-03-04", -1599, "netflix.com monthly", "CAD", None),  # explicit None ok
    ]
    subs = advisor.detect_subscriptions(txns)
    assert len(subs) == 1 and subs[0].merchant == "netflix.com"


def test_leak_report_groups_by_counterparty_when_present():
    txns = [
        ("2026-01-03", -400, "purchase: starbux #1", None, "CAD", "starbucks"),
        ("2026-01-10", -350, "sbux 4471 victoria", None, "CAD", "starbucks"),
    ]
    rows = advisor.leak_report(txns, threshold_minor=1500)
    assert len(rows) == 1
    assert rows[0].merchant == "starbucks" and rows[0].count == 2
