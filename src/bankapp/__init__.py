"""bankapp: local-first personal-finance pipeline.

Layers: an immutable raw_txn ledger (bank truth) with a revisable interpretation
layer on top (categories, transfer/split groups), plus an advisor layer
(net worth, savings, budgets, subscriptions/leaks, goals, digest).
"""

__version__ = "0.0.0"
