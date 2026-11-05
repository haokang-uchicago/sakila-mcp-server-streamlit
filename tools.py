"""
tools.py - the bounded tools over the Sakila star schema (SAKILA.DW).

Both front doors call this one file:
  - server.py wraps each tool as an MCP tool (Claude Code picks and calls them)
  - app.py calls the same functions after DeepSeek picks one
So a question asked in either place runs the same SQL and gets the same number.

Every tool returns a plain dict with the data plus a trace of every SQL it ran.
The app uses the trace to show which tool ran, the SQL, how many rows came back,
what one row represents, and what the query cost.

Signs in as SAKILA_QA_APP / role SAKILA_DW_READER (connection "sakila_qa_app" in
~/.snowflake/config.toml): SELECT on SAKILA.DW only, no write privileges anywhere.
"""
from __future__ import annotations

import atexit
import datetime as dt
import json
import os
import re
import time
from decimal import Decimal
from functools import lru_cache
from typing import Literal

import snowflake.connector

CONNECTION = os.environ.get("SNOWFLAKE_CONNECTION", "sakila_qa_app")
DW = "SAKILA.DW"
MAX_ROWS = 200  # hard cap on the rows any tool hands back

# Things a user might reasonably ask about that this warehouse cannot answer.
# describe_schema returns this list so the model can say "I can't answer that" instead of guessing.
NOT_IN_WAREHOUSE = [
    "actors (the star has no actor dimension; joining film_actor would count each payment once per actor)",
    "inventory or stock levels (how many copies a store owns)",
    "costs, profit or margin (only payment amounts are recorded)",
    "anything after 2006-02-14 or before 2005-05-24 (no payments outside that range)",
]

# ---------------------------------------------------------------------------
# Foundation: one connection, one way to run SQL, one result shape
# ---------------------------------------------------------------------------
_conn = None


def _connection():
    """Open the Snowflake connection once and reuse it (opening one takes about a second)."""
    global _conn
    if _conn is None or _conn.is_closed():
        _conn = snowflake.connector.connect(
            connection_name=CONNECTION,
            session_parameters={"QUERY_TAG": "assignment5"},  # lets QUERY_HISTORY find our queries
            enable_server_session_keep_alive_auto_detection=False,  # log out cleanly on close
        )
    return _conn


@atexit.register
def close() -> None:
    """Close the connection when the program exits."""
    if _conn is not None and not _conn.is_closed():
        _conn.close()


def _clean(value):
    """Turn Snowflake types into plain JSON types (Decimal -> float, dates -> 'YYYY-MM-DD')."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def run_sql(sql: str, params: dict | None = None, limit: int = MAX_ROWS) -> tuple[list[dict], dict]:
    """Run one read-only query and return (rows, trace).

    Values are passed as bound parameters (%(name)s), never pasted into the SQL text,
    so a parameter can never change what the SQL does. Fetches at most `limit` rows.
    """
    cur = _connection().cursor(snowflake.connector.DictCursor)
    started = time.perf_counter()
    cur.execute(sql, params)  # None when there is nothing to bind
    rows = cur.fetchmany(limit + 1)  # one extra row tells us whether the result was cut off
    trace = {
        "sql": sql.strip(),
        "params": {k: _clean(v) for k, v in (params or {}).items()},
        "query_id": cur.sfqid,  # the key into QUERY_HISTORY for elapsed time and cost
        "row_count": min(len(rows), limit),
        "truncated": len(rows) > limit,
        "client_ms": round((time.perf_counter() - started) * 1000),
    }
    rows = [{k.lower(): _clean(v) for k, v in r.items()} for r in rows[:limit]]
    return rows, trace


def _result(tool: str, grain: str, rows: list[dict], queries: list[dict], notes: list[str] | None = None, **extra) -> dict:
    """The one shape every tool returns. `grain` says what one row of `rows` represents."""
    return {
        "tool": tool,
        "grain": grain,
        "row_count": len(rows),
        "rows": rows,
        "notes": notes or [],
        "queries": queries,
        **extra,
    }


# ---------------------------------------------------------------------------
# Tool 1: describe the schema
# ---------------------------------------------------------------------------
def describe_schema() -> dict:
    """Describe the Sakila DVD-rental warehouse: which tables exist, what ONE ROW of each
    table represents (its grain), how many rows each has, and the date range the data
    actually covers.

    Call this first when a question is vague, when you are not sure which table or measure
    answers it, or when the question names a date or year, so you can check that the date
    falls inside the covered range before answering. Also lists what the warehouse does
    NOT contain, so you can say plainly when a question cannot be answered.
    Takes no arguments.
    """
    tables, q1 = run_sql(
        """SELECT table_name, row_count, comment AS grain
FROM SAKILA.INFORMATION_SCHEMA.TABLES
WHERE table_schema = 'DW' AND table_type = 'BASE TABLE'
ORDER BY table_name"""
    )
    coverage, q2 = run_sql(
        f"""SELECT MIN(f.payment_ts)::DATE         AS first_payment_date,
       MAX(f.payment_ts)::DATE           AS last_payment_date,
       COUNT(*)                        AS payments,
       COUNT(DISTINCT c.customer_id)   AS distinct_customers,
       SUM(f.amount)                   AS total_revenue
FROM {DW}.FACT_PAYMENT f
JOIN {DW}.DIM_CUSTOMER c ON c.customer_sk = f.customer_sk"""
    )
    coverage[0]["months_with_data"] = list(data_months())
    notes = [
        "Data covers payments from {first_payment_date} to {last_payment_date} only.".format(**coverage[0]),
        "Only these months have payments: {}. Nothing from 2005-09 to 2006-01, and 2006-02 is a small "
        "tail, so comparing 2006 with 2005 compares a gap with real data.".format(", ".join(data_months())),
        "FACT_PAYMENT has one row per payment, and each rental has exactly one payment, "
        "so counting its rows counts payments (= rentals), not customers.",
        "DIM_CUSTOMER keeps one row per customer per version (SCD Type 2): "
        "count DISTINCT customer_id, not rows, to count customers.",
        "DIM_DATE runs 2005-01-01 to 2006-12-31, wider than the data: a date existing in DIM_DATE "
        "does not mean there are payments on it.",
    ]
    return _result(
        tool="describe_schema",
        grain="one row per table in SAKILA.DW",
        rows=tables,
        queries=[q1, q2],
        notes=notes,
        coverage=coverage[0],
        not_in_warehouse=NOT_IN_WAREHOUSE,
    )


# ---------------------------------------------------------------------------
# Validation: every argument is checked before any SQL is built
# ---------------------------------------------------------------------------
class ToolError(ValueError):
    """A bad argument. The message says what was wrong and what is allowed,
    so the model can fix its call or tell the user plainly."""


# Allow-lists. Only these exact values can reach a WHERE or GROUP BY.
Category = Literal[
    "Action", "Animation", "Children", "Classics", "Comedy", "Documentary", "Drama", "Family",
    "Foreign", "Games", "Horror", "Music", "New", "Sci-Fi", "Sports", "Travel",
]
CATEGORIES = Category.__args__
STORES = {1: "Store 1 (Lethbridge, Canada)", 2: "Store 2 (Woodridge, Australia)"}
Store = Literal[1, 2]
MAX_LIST = 100   # list_payments returns at most this many payment rows
MAX_GROUPS = 50  # aggregate returns at most this many groups


@lru_cache(maxsize=1)
def data_range() -> tuple[dt.date, dt.date]:
    """First and last payment date in the warehouse (asked once, then remembered)."""
    rows, _ = run_sql(f"SELECT MIN(payment_ts)::DATE AS lo, MAX(payment_ts)::DATE AS hi FROM {DW}.FACT_PAYMENT")
    return dt.date.fromisoformat(rows[0]["lo"]), dt.date.fromisoformat(rows[0]["hi"])


@lru_cache(maxsize=1)
def data_months() -> tuple[str, ...]:
    """The months (YYYY-MM) that have at least one payment. The data has a gap:
    nothing from 2005-09 to 2006-01, so a min..max range alone would mislead."""
    rows, _ = run_sql(f"SELECT DISTINCT TO_CHAR(payment_ts, 'YYYY-MM') AS m FROM {DW}.FACT_PAYMENT ORDER BY m")
    return tuple(r["m"] for r in rows)


def _months(start: dt.date, end: dt.date) -> list[str]:
    """Every YYYY-MM from start to end."""
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _parse_date(value, name: str) -> dt.date:
    """Accept only a real calendar date written YYYY-MM-DD."""
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip())
    except ValueError:
        raise ToolError(f"{name} must be a date written YYYY-MM-DD, for example 2005-07-01; got {value!r}.") from None


def _parse_period(start_date, end_date) -> tuple[dt.date, dt.date, list[str]]:
    """Parse the two dates, check their order, and say how they compare with the data we have."""
    start, end = _parse_date(start_date, "start_date"), _parse_date(end_date, "end_date")
    if start > end:
        raise ToolError(f"start_date {start} is after end_date {end}.")
    lo, hi = data_range()
    notes = []
    if end < lo or start > hi:
        notes.append(f"NO DATA for {start} to {end}: the warehouse only has payments from {lo} to {hi}. "
                     "Say there is no data for this period; do not report it as zero.")
    elif start < lo or end > hi:
        notes.append(f"Only part of {start} to {end} has data: payments exist from {lo} to {hi}, "
                     f"so the result really covers {max(start, lo)} to {min(end, hi)}.")
    if not (end < lo or start > hi):
        empty = [m for m in _months(max(start, lo), min(end, hi)) if m not in data_months()]
        if empty:
            notes.append(f"These months inside the period have NO payments at all (a gap in the data, "
                         f"not a drop in business): {', '.join(empty)}.")
    return start, end, notes


def _parse_category(value) -> str | None:
    if value in (None, ""):
        return None
    match = {c.lower(): c for c in CATEGORIES}.get(str(value).strip().lower())
    if match is None:
        raise ToolError(f"Unknown category {value!r}. Allowed: {', '.join(CATEGORIES)}.")
    return match


def _parse_store(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        store = int(value)
    except (TypeError, ValueError):
        store = None
    if store not in STORES:
        raise ToolError(f"Unknown store {value!r}. Allowed: 1 = {STORES[1]}, 2 = {STORES[2]}.")
    return store


def _parse_int(value, name: str, default: int, low: int, high: int) -> int:
    """A whole number, clamped to [low, high] so no call can ask for an unbounded result."""
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a whole number; got {value!r}.") from None
    return max(low, min(high, number))


def _where(start: dt.date, end: dt.date, category: str | None, store: int | None,
           customer_id: int | None = None) -> tuple[str, dict, set[str]]:
    """Build the WHERE clause from already-validated values. Returns (sql, params, dimensions needed).
    Filters on the fact's own date key, so no DIM_DATE join is needed just to filter (Week 4, Part 5)."""
    clauses = ["f.payment_date_key BETWEEN %(start_key)s AND %(end_key)s"]
    params = {"start_key": int(start.strftime("%Y%m%d")), "end_key": int(end.strftime("%Y%m%d"))}
    needs = set()
    if category:
        clauses.append("fl.category = %(category)s")
        params["category"] = category
        needs.add("film")
    if store:
        clauses.append("f.store_key = %(store)s")
        params["store"] = store
    if customer_id:
        clauses.append("c.customer_id = %(customer_id)s")
        params["customer_id"] = customer_id
        needs.add("customer")
    return "\n  AND ".join(clauses), params, needs


JOINS = {  # each dimension the fact can be joined to, in a fixed order
    "date": f"JOIN {DW}.DIM_DATE d      ON d.date_key     = f.payment_date_key",
    "customer": f"JOIN {DW}.DIM_CUSTOMER c  ON c.customer_sk  = f.customer_sk  -- the version valid at payment time",
    "film": f"JOIN {DW}.DIM_FILM fl     ON fl.film_sk     = f.film_sk",
    "store": f"JOIN {DW}.DIM_STORE s     ON s.store_key    = f.store_key",
    "staff": f"JOIN {DW}.DIM_STAFF st    ON st.staff_key   = f.staff_key",
}


def _from(needs: set[str]) -> str:
    return "\n".join([f"FROM {DW}.FACT_PAYMENT f"] + [JOINS[k] for k in JOINS if k in needs])


EMPTY_NOTE = ("Zero rows came back: there is NO DATA matching these filters. "
              "Say 'no data for that', not 'the answer is zero'.")


# ---------------------------------------------------------------------------
# Tool 2: a parameterized query that returns individual payments
# ---------------------------------------------------------------------------
def list_payments(
    start_date: str,
    end_date: str,
    category: Category | None = None,
    store: Store | None = None,
    customer_id: int | None = None,
    limit: int = 20,
) -> dict:
    """List individual payments (one row = one payment = one rental): when it was paid,
    which customer, which film and category, which store, the amount, and days rented.

    Use this to SHOW examples or details ("show me the Action rentals at store 2 on
    2005-07-05", "what did customer 148 rent in August 2005?"). Do NOT use it to count,
    total or average: the list is capped at `limit` rows, so use `aggregate` for any
    "how many / how much / average / top N" question. `total_matching` tells you how many
    payments matched before the cap.

    Arguments:
      start_date, end_date: payment dates, YYYY-MM-DD, inclusive. Data exists 2005-05-24 to 2006-02-14.
      category: optional film category, one of the 16 Sakila categories (e.g. "Action", "Sci-Fi").
      store: optional store, 1 (Lethbridge, Canada) or 2 (Woodridge, Australia).
      customer_id: optional customer number (1 to 599).
      limit: how many rows to return, 1 to 100 (default 20), earliest payments first.
    """
    start, end, notes = _parse_period(start_date, end_date)
    category, store = _parse_category(category), _parse_store(store)
    customer_id = _parse_int(customer_id, "customer_id", 0, 0, 10**9) or None
    limit = _parse_int(limit, "limit", 20, 1, MAX_LIST)

    where, params, _ = _where(start, end, category, store, customer_id)
    params["limit"] = limit
    sql = f"""SELECT f.payment_ts::DATE                    AS payment_date,
       c.customer_id,
       c.first_name || ' ' || c.last_name      AS customer,
       fl.title                                AS film,
       fl.category,
       s.store_name                            AS store,
       f.amount,
       f.rental_days,
       COUNT(*) OVER ()                        AS total_matching
{_from({"customer", "film", "store"})}
WHERE {where}
ORDER BY f.payment_ts, f.payment_id
LIMIT %(limit)s"""
    rows, trace = run_sql(sql, params, limit=limit)
    total = rows[0]["total_matching"] if rows else 0
    for r in rows:
        r.pop("total_matching")
    if not rows:
        notes.append(EMPTY_NOTE)
    elif total > len(rows):
        notes.append(f"Showing the first {len(rows)} of {total} matching payments. "
                     "Use aggregate to count or total them; do not count this list.")
    return _result("list_payments", "one row per payment (= one rental)", rows, [trace], notes,
                   total_matching=total)


# ---------------------------------------------------------------------------
# Tool 3: an aggregate over a period, optionally per group
# ---------------------------------------------------------------------------
Metric = Literal["revenue", "payments", "rentals", "customers", "avg_payment", "avg_rental_days"]
GroupBy = Literal["month", "day_of_week", "category", "rating", "film", "store", "staff", "customer", "country"]

# metric -> (SQL expression, what it measures). The text goes back with every answer.
METRICS = {
    "revenue": ("SUM(f.amount)", "revenue in dollars = SUM of payment amounts"),
    "payments": ("COUNT(*)", "number of payment rows; each rental has one payment. NOT a count of customers"),
    "rentals": ("COUNT(DISTINCT f.rental_id)", "number of rentals (transactions). NOT a count of customers"),
    "customers": ("COUNT(DISTINCT c.customer_id)", "number of distinct customers (people), however many times each paid"),
    "avg_payment": ("AVG(f.amount)", "average dollars per payment"),
    "avg_rental_days": ("AVG(f.rental_days)", "average days a film was kept; rentals not yet returned are left out"),
}
# group -> (SELECT columns, GROUP BY columns, ORDER BY, dimensions needed, what one row is)
GROUPS = {
    "month": ("d.year_month AS month", "d.year_month", "d.year_month", {"date"}, "one row per calendar month"),
    "day_of_week": ("d.day_name AS day_of_week", "d.day_of_week, d.day_name", "d.day_of_week", {"date"},
                    "one row per day of the week (Mon..Sun)"),
    "category": ("fl.category", "fl.category", "value DESC", {"film"}, "one row per film category"),
    "rating": ("fl.rating", "fl.rating", "value DESC", {"film"}, "one row per film rating (G, PG, ...)"),
    "film": ("fl.title AS film", "fl.film_id, fl.title", "value DESC", {"film"}, "one row per film"),
    "store": ("s.store_name AS store", "s.store_name", "value DESC", {"store"}, "one row per store"),
    "staff": ("st.staff_name AS staff", "st.staff_name", "value DESC", {"staff"},
              "one row per staff member who took the payment"),
    "customer": ("c.customer_id, ANY_VALUE(c.first_name || ' ' || c.last_name) AS customer", "c.customer_id",
                 "value DESC", {"customer"}, "one row per customer"),
    "country": ("c.country", "c.country", "value DESC", {"customer"},
                "one row per customer country (where the customer lived when paying)"),
}


def aggregate(
    metric: Metric,
    start_date: str,
    end_date: str,
    group_by: GroupBy | None = None,
    category: Category | None = None,
    store: Store | None = None,
    top_n: int = 10,
) -> dict:
    """Compute one number for a period, or one number per group: revenue, counts, or averages.

    Use this for every "how much / how many / average / top N / by month / by category"
    question. Pick the metric that matches the question's NOUN:
      - "revenue", "sales", "how much money"           -> metric="revenue"
      - "how many customers / people / renters"        -> metric="customers" (distinct people)
      - "how many rentals / transactions / times"      -> metric="rentals"
      - "how many payments"                            -> metric="payments"
      - "average payment / spend per rental"           -> metric="avg_payment"
      - "how long are films kept / rental duration"    -> metric="avg_rental_days"
    "customers" and "rentals" differ by about 27x in this data, so never answer a customers
    question with rentals or payments.

    Arguments:
      metric: one of revenue, payments, rentals, customers, avg_payment, avg_rental_days.
      start_date, end_date: payment dates, YYYY-MM-DD, inclusive. Data exists 2005-05-24 to 2006-02-14.
      group_by: optional; one of month, day_of_week, category, rating, film, store, staff,
                customer, country. Leave empty for a single total.
      category: optional film category filter (e.g. "Comedy").
      store: optional store filter, 1 (Lethbridge, Canada) or 2 (Woodridge, Australia).
      top_n: for ranked groups (category, film, customer, ...), how many to return, 1 to 50 (default 10).
             month and day_of_week are returned in calendar order.
    """
    if metric not in METRICS:
        raise ToolError(f"Unknown metric {metric!r}. Allowed: {', '.join(METRICS)}.")
    if group_by in ("", None):
        group_by = None
    elif group_by not in GROUPS:
        raise ToolError(f"Unknown group_by {group_by!r}. Allowed: {', '.join(GROUPS)}, or leave it empty.")
    start, end, notes = _parse_period(start_date, end_date)
    category, store = _parse_category(category), _parse_store(store)
    top_n = _parse_int(top_n, "top_n", 10, 1, MAX_GROUPS)

    expr, meaning = METRICS[metric]
    where, params, needs = _where(start, end, category, store)
    if metric == "customers":
        needs.add("customer")
    if group_by:
        select, group, order, more, grain = GROUPS[group_by]
        needs |= more
        params["top_n"] = top_n
        sql = f"""SELECT {select},
       {expr} AS value,
       COUNT(*) AS payments_in_group
{_from(needs)}
WHERE {where}
GROUP BY {group}
ORDER BY {order}
LIMIT %(top_n)s"""
    else:
        grain = "one row: the whole period"
        sql = f"""SELECT {expr} AS value,
       COUNT(*) AS payments_in_group
{_from(needs)}
WHERE {where}"""
    rows, trace = run_sql(sql, params, limit=MAX_GROUPS)
    if rows and rows[0]["payments_in_group"] == 0:  # an ungrouped aggregate over nothing still returns one row
        rows = []
    for r in rows:
        if isinstance(r["value"], float):
            r["value"] = round(r["value"], 2)
    if not rows:
        notes.append(EMPTY_NOTE)
    if group_by and order == "value DESC" and len(rows) == top_n:
        notes.append(f"Only the top {top_n} groups are shown.")
    return _result("aggregate", grain, rows, [trace], notes, metric=metric, value_means=meaning)


# ---------------------------------------------------------------------------
# Tool 4: what the queries cost
# ---------------------------------------------------------------------------
# Credits per hour a running warehouse burns, by size (Snowflake standard warehouses).
CREDITS_PER_HOUR = {"X-Small": 1, "Small": 2, "Medium": 4, "Large": 8, "X-Large": 16,
                    "2X-Large": 32, "3X-Large": 64, "4X-Large": 128, "5X-Large": 256, "6X-Large": 512}
QUERY_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_IDS = 20


def query_cost(query_ids: list[str] | None = None, last_n: int = 5) -> dict:
    """Report what Snowflake queries cost: elapsed time (split into compile, queue and
    execution), bytes scanned, rows produced, warehouse size, and credits, read from
    INFORMATION_SCHEMA.QUERY_HISTORY.

    Every other tool returns the query_id of each SQL it ran (in `queries`). Pass those ids
    to see what that answer cost. With no ids, returns the last `last_n` queries this app ran.

    Credits: Snowflake reports cloud-services credits per query, but not warehouse credits,
    because it bills a warehouse for the time it is running, not per query. So warehouse
    credits here are an ESTIMATE: execution time x the size's credits per hour
    (X-Small = 1 credit/hour). Exact per-query figures exist in
    SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY, but they arrive hours later and need
    extra grants, so this tool does not use them.

    Arguments:
      query_ids: optional list of Snowflake query ids (up to 20), e.g. from a tool's `queries`.
      last_n: when no ids are given, how many recent queries to show, 1 to 20 (default 5).
    """
    if isinstance(query_ids, str):
        query_ids = [query_ids]
    bad = [q for q in (query_ids or []) if not QUERY_ID.match(str(q).strip().lower())]
    if bad:
        raise ToolError(f"Not a Snowflake query id: {bad[0]!r}. Ids look like 01c828cb-0103-cccf-001f-81ef001a821a.")
    query_ids = [str(q).strip().lower() for q in (query_ids or [])]
    if len(query_ids) > MAX_IDS:
        raise ToolError(f"At most {MAX_IDS} query ids at a time; got {len(query_ids)}.")
    last_n = _parse_int(last_n, "last_n", 5, 1, MAX_IDS)

    select = """SELECT query_id,
       start_time,
       execution_status                                                  AS status,
       LEFT(query_text, 80)                                              AS sql_start,
       total_elapsed_time                                                AS elapsed_ms,
       compilation_time                                                  AS compile_ms,
       queued_provisioning_time + queued_overload_time + queued_repair_time AS queued_ms,
       execution_time                                                    AS execution_ms,
       bytes_scanned,
       rows_produced,
       warehouse_size,
       credits_used_cloud_services
FROM TABLE(SAKILA.INFORMATION_SCHEMA.QUERY_HISTORY_BY_USER(RESULT_LIMIT => 1000))"""
    if query_ids:
        sql = select + "\nWHERE query_id IN (%(ids)s)\nORDER BY start_time"
        params = {"ids": query_ids}
    else:  # this app's own recent queries, leaving out these cost lookups
        sql = select + """
WHERE query_tag = 'assignment5'
  AND query_type = 'SELECT'
  AND NOT CONTAINS(query_text, 'QUERY_HISTORY')
ORDER BY start_time DESC
LIMIT %(last_n)s"""
        params = {"last_n": last_n}

    for attempt in range(4):  # a query can take a moment to show up in the history
        rows, trace = run_sql(sql, params, limit=MAX_IDS)
        if not query_ids or len(rows) >= len(set(query_ids)):
            break
        time.sleep(0.5)
    if not query_ids:
        rows.reverse()  # oldest first, like the list-of-ids case

    notes = []
    missing = sorted(set(query_ids) - {r["query_id"] for r in rows})
    if missing:
        notes.append(f"Not found in this app's recent query history: {', '.join(missing)} "
                     "(it may belong to another user, or be older than the last 1000 queries / 7 days).")
    for r in rows:
        r["sql_start"] = " ".join((r["sql_start"] or "").split())
        # No warehouse size and nothing scanned: Snowflake reused a stored result and never woke the warehouse.
        r["from_result_cache"] = r["warehouse_size"] is None and not r["bytes_scanned"]
        rate = CREDITS_PER_HOUR.get(r["warehouse_size"], 0)
        r["est_warehouse_credits"] = round(r["execution_ms"] / 3_600_000 * rate, 7)
        r["credits_used_cloud_services"] = round(r["credits_used_cloud_services"] or 0, 7)
        r["est_total_credits"] = round(r["est_warehouse_credits"] + r["credits_used_cloud_services"], 7)
    totals = {
        "queries": len(rows),
        "elapsed_ms": sum(r["elapsed_ms"] for r in rows),
        "est_total_credits": round(sum(r["est_total_credits"] for r in rows), 7),
    }
    if any(r["from_result_cache"] for r in rows):
        notes.append("Some results came from Snowflake's result cache (the same query ran recently): "
                     "no warehouse time was used, so their warehouse credits are 0.")
    if any(r["queued_ms"] for r in rows):
        notes.append("Some queries waited in a queue first (queued_ms), usually for a suspended warehouse to wake up.")
    if rows:
        notes.append("Warehouse credits are estimates (execution time x credits per hour for the warehouse size). "
                     "Snowflake really bills a warehouse per second while it runs, with a 60-second minimum "
                     "each time it wakes up, so the bill for one query after the warehouse slept is larger. "
                     "Cloud-services credits are billed only when they exceed 10% of the day's warehouse credits.")
    else:
        notes.append(EMPTY_NOTE)
    return _result("query_cost", "one row per query that ran in Snowflake", rows, [trace], notes, totals=totals)


TOOLS = [describe_schema, list_payments, aggregate, query_cost]


if __name__ == "__main__":  # quick check: python tools.py
    def show(label, call):
        try:
            r = call()
            print(f"\n== {label}\n   tool={r['tool']} | grain: {r['grain']} | rows={r['row_count']}")
            for row in r["rows"][:5]:
                print("  ", row)
            for n in r["notes"]:
                print("   note:", n)
        except ToolError as e:
            print(f"\n== {label}\n   REFUSED: {e}")

    show("schema", describe_schema)
    show("revenue by category, June 2005", lambda: aggregate("revenue", "2005-06-01", "2005-06-30", group_by="category", top_n=3))
    show("customers, whole period", lambda: aggregate("customers", "2005-05-24", "2006-02-14"))
    show("rentals, whole period", lambda: aggregate("rentals", "2005-05-24", "2006-02-14"))
    show("revenue by month, 2005-08 to 2006-02 (gap)", lambda: aggregate("revenue", "2005-08-01", "2006-02-28", group_by="month"))
    show("revenue in 2007 (no data)", lambda: aggregate("revenue", "2007-01-01", "2007-12-31"))
    show("Action payments at store 2 on 2005-07-05", lambda: list_payments("2005-07-05", "2005-07-05", category="action", store=2, limit=3))
    show("bad group_by", lambda: aggregate("revenue", "2005-06-01", "2005-06-30", group_by="actor"))
    show("SQL in a parameter", lambda: aggregate("revenue", "2005-06-01'; DROP TABLE x; --", "2005-06-30"))

    # Cost: run one answer, then ask what its SQL cost. Run it twice: the second time comes from the result cache.
    for label in ("asked once", "asked again"):  # "asked once" is cached too if run in the last 24 hours
        answer = aggregate("revenue", "2005-07-01", "2005-07-31", group_by="store")
        show(f"cost of 'revenue by store, July 2005' ({label})",
             lambda: query_cost([q["query_id"] for q in answer["queries"]]))
    show("cost of the last 3 queries", lambda: query_cost(last_n=3))
    show("bad query id", lambda: query_cost(["abc; DROP TABLE x"]))
    close()
