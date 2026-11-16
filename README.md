# Snowflake Planning Pipeline and AI Assistant

ZAAR sells medical apparel across several channels, including hospitals, retail partners and online. Each planning cycle, the **production manager** needs to know how many finished goods to make, and the **supply manager** needs to know how much material to reorder. I worked on building the data pipeline with a team, and worked solely on the chatbot behind those answers in Snowflake.

## About this repo

The real system runs on Snowflake with confidential business data, so it can't be published. This repo is a **runnable prototype of the same design**: every number is synthetic, Snowflake is replaced by a local `sqlite` database, and the LLM is replaced by a deterministic stand-in so the evaluation is reproducible with zero credentials.

It emphasizes two things: the **design of the system and its data workflow**, and the **effectiveness of the chatbot and how it is evaluated**.

---

## 1. Business objective and system boundaries

- **Production manager:** needs next-cycle finished-goods production quantities.
- **Supply manager:** needs material reorder quantities.
- **The chatbot's job:** *explain approved results.* It does **not** invent forecasts or independently decide production plans. All numbers come from deterministic SQL; the LLM only chooses which approved lookup to run and phrases the result.

That boundary is the core design decision: in an operations setting a fluent wrong number is worse than no answer, so the system is built to **fail closed**.

## 2. System architecture

![System architecture: source systems and ingestion feed a Snowflake pipeline (RAW, classify, change processing, curated, serve) with a quarantine error table; the assistant application reads the serve layer through allowlisted tools; alerts and evaluation run across everything.](docs/images/system_architecture.png)

The fallback dashboard is a **parallel path**, not a downstream chatbot output: if the chatbot is down or blocked, managers still have a validated snapshot. More views in [`docs/architecture.md`](docs/architecture.md): the **request lifecycle** (every point where a question can be refused, retried or blocked), the **data model** (entity-relationship diagram) and the **trust boundaries and role access** table.

## 3. End-to-end data pipeline

| Stage | Implementation and engineering controls |
|---|---|
| **Ingest** | Shopify orders/cancellations via Windsor.ai; hospital B2B orders via scheduled Google Sheets ingestion; retailer/vendor files via S3 + Snowpipe. Land immutable raw records with source IDs, ingestion time and batch IDs. |
| **Standardize** | `LEFT JOIN` source product identifiers to the canonical SKU map; normalize units; deduplicate by source order/line ID; **classify every row as valid or rejected with a reason**, and quarantine unmapped SKUs and malformed records instead of dropping them (see [error handling](#error-handling-for-unmapped-skus-and-malformed-records)). |
| **Process changes** | Snowflake Streams identify changed rows; Tasks run idempotent `MERGE` updates for cancellations, returns and corrected orders; alert on failed jobs and reconcile record counts. |
| **Calculate** | Dynamic Tables refresh curated inventory and replenishment views at defined freshness targets; demand forecasts, BOM, batch sizes, capacity and lead times feed deterministic SQL. |
| **Serve** | Read-only production and supply views expose quantities, SKU/material, cycle, units, record ID, source version and calculation timestamp. A daily validated snapshot feeds an independent fallback dashboard. |

### Error handling for unmapped SKUs and malformed records

The principle: **RAW accepts everything, and every landed row ends up in exactly one place.** Nothing is rejected at the door and nothing is silently dropped.

```
landed row ──► classify (LEFT JOIN sku_map + validity rules)
                 ├─ valid     ──► MERGE into order_lines ──► curated / serve
                 └─ rejected  ──► QUARANTINE (reason, status OPEN) ──► alert
                                     └─ fix the SKU map ──► replay as a new batch ──► RESOLVED

landing rows  =  applied  +  duplicates  +  quarantined        (reconciled on every run)
```

| Reject reason | Example | Recoverable by | 
|---|---|---|
| `UNMAPPED_SKU` | A new Shopify variant that is not in the SKU map yet | Add the mapping row, then the replay task re-lands it |
| `MISSING_KEY` / `MISSING_SKU` | Blank order ID or SKU in a Sheets row | Correct the source and re-ingest |
| `NULL_QUANTITY` / `NEGATIVE_QUANTITY` | Empty or `-5` quantity | Correct the source and re-ingest |
| `UNKNOWN_EVENT_TYPE` | A `refund` event the pipeline does not model | Decide the business rule, then extend the model |
| `MISSING_TIMESTAMP` | Order with no event time | Correct the source and re-ingest |

Design choices worth calling out:

- **`LEFT JOIN`, not `INNER JOIN`.** An inner join to the SKU map silently drops unmapped rows, which are exactly the rows that need attention. With a left join they survive with a `NULL` canonical SKU and get a reason.
- **The left join has a trap, and it is checked.** If the SKU map contains a duplicated key, the join fans out and multiplies rows. Snowflake does not enforce primary keys, so the pipeline checks map uniqueness and that classified rows equal landed rows.
- **One transaction.** Quarantine inserts and the `MERGE` run together in a single procedure reading the stream. Two separate tasks on one stream would be wrong: the first statement advances the stream offset and the second would see nothing.
- **Replay keeps RAW immutable.** A fixed row is re-landed as a new `REPLAY-...` batch rather than edited in place.
- **Alerts:** reject rate above a threshold (a sudden jump usually means a broken upstream export), and unmapped SKUs left open longer than 48 hours. Thresholds are illustrative.

Runnable version with tests: [`ingest.py`](src/ops_assistant/ingest.py), [`test_ingest.py`](tests/test_ingest.py), sample bad rows in [`orders_landing.csv`](data/synthetic/orders_landing.csv). Snowflake version: [`sql/01`](sql/01_raw_and_sku_mapping.sql) and [`sql/02`](sql/02_streams_tasks_merge.sql).

Code: [`sql/01_raw_and_sku_mapping.sql`](sql/01_raw_and_sku_mapping.sql) · [`02_streams_tasks_merge.sql`](sql/02_streams_tasks_merge.sql) · [`03_dynamic_tables.sql`](sql/03_dynamic_tables.sql) · [`04_planning_views.sql`](sql/04_planning_views.sql)

## 4. Deterministic planning calculations

```
net finished-goods requirement = max(0, forecast demand + safety stock − available stock − scheduled receipts)
executable production          = round net UP to a valid batch size, capped by capacity (whole batches);
                                 if capacity is insufficient → flag a planning exception, never silently change it
material reorder               = max(0, planned production × BOM usage + material safety stock
                                        − available material − scheduled receipts)
                                 → rounded up to supplier pack size, at least the minimum order quantity
```

**Worked example (synthetic):** 500 demand + 100 safety − 180 on hand − 50 incoming = **370** net units → rounded up to **400** with 100-unit batches. Implemented in [`planning.py`](src/ops_assistant/planning.py) (Python mirror of the SQL) and pinned by a test in [`tests/test_planning.py`](tests/test_planning.py). This exact example is also the story of iteration V4 below.

## 5. Controlled retrieval and request-time controls

Streamlit collects the question and the authenticated user context. The model **never gets arbitrary SQL execution rights**:

- It can only select among **allowlisted, parameterized, read-only tools**: `get_production_plan`, `get_inventory_status`, `get_material_reorder` ([`tools.py`](src/ops_assistant/tools.py)).
- The **server** validates parameters (known SKU / material / cycle) and enforces role-based access *before* touching data. A production manager asking a supply question is refused, with no data leaked.
- Tools return **structured evidence**: metric, SKU/material, cycle, unit, record ID, source timestamp.

**Before retrieval** ([`router.py`](src/ops_assistant/router.py)): authenticate → authorize role and data domain → classify the request as fully in-scope / mixed / out-of-scope → normalize SKU and period → reject invalid parameters (including prompt-injection / raw-SQL attempts).

**After generation** ([`validator.py`](src/ops_assistant/validator.py)): deterministic Python checks **each numerical claim against the exact metric + SKU/material + cycle + unit + source record**, not merely whether the number appears somewhere in the results. It also checks that mandatory qualifiers are present (e.g. a capacity exception can't be dropped). Only validated answers are displayed.

A correct number attached to the wrong SKU or period is still a wrong answer; this is the failure mode the validator is designed around (see [`test_validator.py`](tests/test_validator.py)).

## 6. Failure handling, access and observability

| Condition | Safe behavior | Where |
|---|---|---|
| Unmapped SKU or malformed record upstream | Land it, quarantine it with a reason, keep it out of planning data, alert on reject rate and age; replay after the map is fixed | `sql/01`, `sql/02`, `ingest.py` |
| Bad/missing SKU or unauthorized scope | Ask for clarification or refuse; no cross-role data leakage | `router.py`, `tools.py` |
| Query timeout / transient tool error | Bounded retry with backoff (2 attempts), then safe failure; never invent values | `controls.with_retry` |
| Validation mismatch / validator exception | Block the answer; one controlled regeneration if evidence is valid, otherwise fail closed and log an alert | `answer.py` |
| Stale inputs / broken upstream job | Freshness SLA (< 24h for daily planning); show timestamp and block the recommendation if breached | `controls.stale_records` |
| Dashboard fallback | Last successfully validated snapshot, with as-of time and staleness warning; escalate if outside the decision-safe window | `sql/03_dynamic_tables.sql` |

## 7. The chatbot in action

Synthetic outputs from this repo (`production_manager` unless noted):

```text
Q: How many scrub tops should we produce next cycle?
A: **SCR-TOP-001 (2026-C12): produce 400 units** [INV-0001+FC-0001]
   - Net requirement: 370 units
   _Source data as of 2026-11-15T05:00:00._

Q: How many scrub pants should we make this cycle?
A: **SCR-PNT-001 (2026-C12): produce 500 units** [INV-0002+FC-0002]
   - Net requirement: 550 units
   - Capacity exception: requirement exceeds cycle capacity by 100 units, so production is capped at 500 units.

Q: What are the safety stock levels for all SKUs?                                 ← one of the default questions
A: - **SCR-TOP-001** (2026-C12): safety stock 100 units [FC-0001]
   - **SCR-PNT-001** (2026-C12): safety stock 150 units [FC-0002]
   - **LAB-COT-001** (2026-C12): safety stock 40 units [FC-0003]

Q: How many scrub tops should we produce, and what will demand be in 2027?      ← mixed → partial answer
A: **SCR-TOP-001 (2026-C12): produce 400 units** [INV-0001+FC-0001] ... **Not covered:** new demand forecasts or
   predictions are outside this assistant's scope (it explains approved results only).

Q: How much BTN-STD should we order?                                             ← wrong role
A: You don't have access to that data domain with your current role.
```

## 8. Reliability improvement story

Five iterations, each driven by a failure the previous version could not see:

**V1: Automated source reconciliation.** Source-grounding from day one: every generated quantity had to match the retrieved Snowflake record on metric, SKU, cycle and unit; unsupported claims were blocked before display. Tests deliberately swapped SKUs, periods and units. → [`validator.py`](src/ops_assistant/validator.py), the `swap_*` cases in [`eval/failure_injection.py`](eval/failure_injection.py)

**V2: Manual evaluation and error discovery.** Built a ~35-question evaluation set and compared answers by hand with the authoritative Snowflake tables. Manual review exposed problems that numerical matching alone missed: omitted qualifiers, incomplete responses, stale evidence. Automated checks were expanded and each finding became a regression case. → [`eval/questions.yaml`](eval/questions.yaml), [`eval/manual_review_template.csv`](eval/manual_review_template.csv)

**V3: Gray-zone questions and scope handling.** Added a third behavior between *answer* and *refuse*: **partial answer**. For mixed requests the assistant answers only the supported part and explicitly says what is not covered; fully unsupported forecasting questions are refused. → Q19–Q22 in the eval set

**V4: Production-manager feedback and planning logic.** A manager flagged a mathematically valid but operationally unusable recommendation (370 units despite 100-unit production batches). I investigated the upstream assumptions, added batch-size and capacity constraints in SQL, and tested the corrected result; the chatbot then retrieved the revised approved quantity (**400**). Note the fix was in the *planning logic*, not the chatbot. → `v4_regression_batch_rounding_370_to_400` in [`eval/failure_injection.py`](eval/failure_injection.py)

**V5: Manager trial and usability.** I had the managers use the bot and incorporated their feedback. *Length and format:* answers now lead with the number they asked for in bold, put supporting figures in short bullets, and collapse to one line per item for list questions (capped by a test). *Default questions:* the chat UI shows one-click buttons for the questions they ask every cycle, including **safety stock levels**, which needed a new allowlisted tool per role. Both are pinned by tests, and every default question is checked to return a validated answer. → [`generator.py`](src/ops_assistant/generator.py), [`suggestions.py`](src/ops_assistant/suggestions.py), Q33–Q36 in the eval set

## 9. Evaluation and release gates

| Measure | How it is tested |
|---|---|
| Numerical and citation accuracy | Claim-to-record matching with SKU, period, metric and units; source lineage check |
| Scope / partial-answer correctness | Labeled in-scope, mixed and out-of-scope questions; compare expected response class |
| Completeness and operational validity | Review against approved source tables, batch rules and domain-manager feedback |
| Reliability and regression | Tool timeout, stale data, unauthorized access, validator exceptions, changed orders and replay tests |

Run it: `python eval/run_eval.py` (exits non-zero if any gate fails; `pytest` runs it too, so a regression blocks the build).

> **Read this scorecard honestly.** It is run against a deterministic stand-in generator and a rule-based router on synthetic data, so 100% mainly demonstrates that the *harness and controls work* (e.g. tampered answers with swapped SKUs, periods, units, wrong values, omitted qualifiers and invented numbers are all blocked). It is **not** the measured accuracy of the production LLM, which is evaluated separately against real Snowflake tables.

## 10. Run it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

pytest -q                          # unit tests + the evaluation suite as a release gate
python eval/run_eval.py            # print the gate scorecard

pip install streamlit              # optional: chat UI
streamlit run app/streamlit_app.py
```

## Repo layout

A quick map of where things live:

```
README.md
docs/system_design.pdf          original design document
sql/                            Snowflake-dialect pipeline + planning views (illustrative)
data/synthetic/                 made-up forecast, inventory, BOM, batch rules, materials
src/ops_assistant/             ingest (classify · quarantine · replay) · planning · tools (allowlist + RBAC) · router · generator · validator · controls · answer
app/streamlit_app.py            chat UI
eval/                           questions.yaml · run_eval.py · failure_injection.py · manual review template
tests/                          pytest suite
```
