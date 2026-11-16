"""Default questions shown as one-click buttons, per role.

Chosen with the managers after trial use: these are the questions they ask every cycle
(safety stock levels in particular). Every entry is checked by tests/test_suggestions.py to
confirm it routes to an in-scope answer for its role, so the buttons can never go stale.
"""
from __future__ import annotations

DEFAULT_QUESTIONS: dict[str, list[str]] = {
    "production_manager": [
        "How many scrub tops should we produce?",
        "How many scrub pants should we make?",
        "What are the safety stock levels for all SKUs?",
        "Show the production plan for all SKUs",
        "What's the inventory for all SKUs?",
    ],
    "supply_manager": [
        "How much blue polyester fabric should we order?",
        "Show material reorders for all materials",
        "What are the safety stock levels for all materials?",
        "What's the supplier lead time for buttons?",
    ],
}
