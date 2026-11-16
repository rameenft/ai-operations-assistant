"""Request-time controls: scope classification, entity/period normalisation, tool selection.

Production uses a Cortex LLM to pick among the allowlisted tools. This prototype uses
deterministic rules so the evaluation suite is reproducible and runs without credentials.
Either way the output is the same contract: a list of (tool_name, params) drawn from the
allowlist in tools.py - never SQL - plus the scope class (in / mixed / out).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ALIASES = {
    "scrub top": "SCR-TOP-001", "scrub pant": "SCR-PNT-001", "lab coat": "LAB-COT-001",
    "polyester": "FAB-POLY-BLU", "cotton": "FAB-COT-WHT", "button": "BTN-STD",
}

_ID = re.compile(r"\b[A-Z]{2,4}(?:-[A-Z0-9]{2,4})+\b")
_CYCLE = re.compile(r"\b\d{4}-C\d{1,2}\b")
_ALL = re.compile(r"\b(all|every|each)\b", re.I)

INTENTS = {
    "safety": re.compile(r"\b(safety[- ]stock\w*|buffer stock|stock buffer)\b", re.I),
    "material": re.compile(r"\b(re-?order\w*|order|purchas\w*|buy|procure\w*|material\w*|fabric\w*|button\w*|lead time)\b", re.I),
    "production": re.compile(r"\b(produc\w*|make|made|manufactur\w*|plan|batch\w*|net requirement)\b", re.I),
    "inventory": re.compile(r"\b(inventory|stock|on[- ]hand|incoming|scheduled receipts?|in stock)\b", re.I),
}

# (pattern, reason shown to the user). Order matters: injection first.
OUT_OF_SCOPE = [
    (re.compile(r"(ignore (all |any )?(previous|prior) instructions|\bselect\b.+\bfrom\b|\bdrop\s+table\b|\bdelete\s+from\b)", re.I),
     "arbitrary SQL or instruction overrides are not permitted"),
    (re.compile(r"(\bpredict\w*|\bwhat will\b.*\b(demand|sales)\b|\bnext (year|quarter|month)\b|\b20(2[7-9]|3\d)\b|\bforecast\w* (for|the) (next|20))", re.I),
     "new demand forecasts or predictions are outside this assistant's scope (it explains approved results only)"),
    (re.compile(r"\b(price|prices|pricing|competitor\w*|marketing|salary|salaries|hiring|weather|stock market)\b", re.I),
     "that topic is outside production and supply planning"),
]

# Weak words ("fabric", "stock") also appear in safety-stock questions; only these strong verbs
# keep a second intent alive next to "safety".
STRONG = {
    "production": re.compile(r"\b(produc\w*|make|made|manufactur\w*|net requirement)\b", re.I),
    "material": re.compile(r"\b(re-?order\w*|order|purchas\w*|buy|procure\w*|lead time)\b", re.I),
}

TOOL_FOR_INTENT = {
    "safety": ("get_safety_stock", "sku"),
    "production": ("get_production_plan", "sku"),
    "inventory": ("get_inventory_status", "sku"),
    "material": ("get_material_reorder", "material"),
}


@dataclass
class RoutePlan:
    scope: str                                   # "in" | "mixed" | "out"
    action: str                                  # "retrieve" | "refuse" | "clarify"
    calls: list[tuple[str, dict]] = field(default_factory=list)
    not_covered: list[str] = field(default_factory=list)
    message: str = ""


def route(question: str, catalog: dict, role: str | None = None) -> RoutePlan:
    q = question.strip()
    scan = _CYCLE.sub(" ", q)  # a cycle label like 2027-C03 is a period, not "the year 2027"

    not_covered = [reason for pat, reason in OUT_OF_SCOPE if pat.search(scan)]
    intents = [name for name, pat in INTENTS.items() if pat.search(q)]
    if "safety" in intents:
        intents = ["safety"] + [i for i in intents if i in STRONG and STRONG[i].search(q)]
    if "material" in intents and "production" in intents:
        intents.remove("production")  # "order fabric for production" is a material question

    if not_covered and "arbitrary SQL" in not_covered[0]:
        return RoutePlan("out", "refuse", not_covered=not_covered[:1],
                         message="I can't do that: " + not_covered[0] + ".")
    if not intents:
        reason = not_covered[0] if not_covered else "I can only answer approved production, inventory and material-reorder questions"
        return RoutePlan("out", "refuse", not_covered=[reason], message=f"I can't help with that: {reason}.")

    scope = "mixed" if not_covered else "in"

    # --- normalise entities and period ---
    ids = _ID.findall(q)
    known = catalog["skus"] | catalog["materials"]
    unknown = [i for i in ids if i not in known]
    if unknown:
        return RoutePlan(scope, "clarify", message=f"I don't recognise {', '.join(unknown)}. Which SKU or material do you mean?")
    skus = [i for i in ids if i in catalog["skus"]]
    mats = [i for i in ids if i in catalog["materials"]]
    low = q.lower()
    for alias, ident in ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}s?\b", low):
            (skus if ident in catalog["skus"] else mats).append(ident)
    skus, mats = list(dict.fromkeys(skus)), list(dict.fromkeys(mats))
    if _ALL.search(q):
        skus = skus or sorted(catalog["skus"])
        mats = mats or sorted(catalog["materials"])

    cycles = _CYCLE.findall(q)
    if cycles and cycles[0] not in catalog["cycles"]:
        return RoutePlan(scope, "clarify",
                         message=f"I have no approved plan for {cycles[0]}. Available cycle(s): {', '.join(sorted(catalog['cycles']))}.")
    cycle = cycles[0] if cycles else catalog["default_cycle"]

    # --- choose tools ---
    calls = []
    for intent in intents:
        tool, kind = TOOL_FOR_INTENT[intent]
        if intent == "safety":  # one intent, two data domains: decide by role (the domain the caller may see)
            named_only_skus = bool(skus) and not mats
            kind = "material" if (mats and not skus) or (role == "supply_manager" and not named_only_skus) else "sku"
            tool = "get_material_safety_stock" if kind == "material" else "get_safety_stock"
        entities = skus if kind == "sku" else mats
        if not entities:
            label = "SKU" if kind == "sku" else "material"
            return RoutePlan(scope, "clarify", message=f"Which {label} do you mean? (or say 'all')")
        calls += [(tool, {kind: e, "cycle": cycle}) for e in entities]

    return RoutePlan(scope, "retrieve", calls=calls, not_covered=not_covered)
