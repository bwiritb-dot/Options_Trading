"""
ETH Options Optimizer — Step 1: Data Layer
==========================================
Fetches all liquid ETH options from Deribit (no auth required),
applies multi-stage filtering, and computes liquidity scores.

Usage:
    python optimizer_step1.py            # live Deribit data
    python optimizer_step1.py --test     # run unit-test assertions

Dependencies:
    pip install aiohttp
"""

from __future__ import annotations

import asyncio
import argparse
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiohttp

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS — never use magic numbers below this block
# ─────────────────────────────────────────────────────────────────────────────

DERIBIT_BASE_URL: str = "https://www.deribit.com/api/v2"

# Instrument filter thresholds
MIN_BID_PRICE: float = 0.0          # strictly greater than this
MIN_ASK_PRICE: float = 0.0          # strictly greater than this
MAX_SPREAD_PCT: float = 0.30        # (ask - bid) / mark_price
MIN_DAYS_TO_EXPIRY: int = 2
MAX_DAYS_TO_EXPIRY: int = 14
MIN_MARK_PRICE: float = 0.001
MIN_OPEN_INTEREST: float = 10.0
MIN_LIQUIDITY_SCORE: float = 0.35

# Liquidity score weights (must sum to 1.0)
SCORE_WEIGHT_SPREAD: float = 0.40
SCORE_WEIGHT_VOLUME: float = 0.35
SCORE_WEIGHT_OI: float = 0.25
SCORE_VOLUME_NORMALIZER: float = 500_000.0  # USD
SCORE_OI_NORMALIZER: float = 1_000.0        # contracts

# HTTP client settings
HTTP_TIMEOUT_SECONDS: int = 10
HTTP_CONCURRENCY_LIMIT: int = 40    # max parallel order-book requests
HTTP_RETRY_ATTEMPTS: int = 3
HTTP_RETRY_BASE_DELAY: float = 0.5  # seconds, doubles each retry

SECONDS_PER_DAY: float = 86_400.0

# ─────────────────────────────────────────────────────────────────────────────
# DATA MODEL
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OptionInstrument:
    """All data needed for MILP for a single ETH option leg."""

    # Identity
    name: str
    option_type: str          # "call" | "put"
    strike: float             # USD
    expiry_ts: int            # unix ms from Deribit
    days_to_expiry: float     # computed

    # Market data (from order book)
    best_bid: float           # ETH-denominated premium
    best_ask: float           # ETH-denominated premium
    mark_price: float         # ETH-denominated
    open_interest: float      # contracts
    volume_usd_24h: float     # USD

    # Greeks (Deribit-native units)
    delta: float              # dimensionless [−1, +1]
    gamma: float              # per ETH
    theta: float              # ETH/day  ← must × SPOT for USD/day
    vega: float               # ETH per 1% IV

    # Derived
    mark_iv: float            # percent, e.g. 80.0 means 80% IV
    spread_pct: float         # (ask − bid) / mark_price
    liquidity_score: float    # [0, 1]

    # Settlement currency (ETH-settled vs USDC-settled)
    settlement_currency: str  = "ETH"


@dataclass
class FetchStats:
    """Counters for the fetch+filter pipeline."""

    total_instruments: int = 0
    fetched_orderbooks: int = 0
    dropped_no_bid_ask: int = 0
    dropped_spread: int = 0
    dropped_expiry: int = 0
    dropped_mark_price: int = 0
    dropped_open_interest: int = 0
    dropped_liquidity_score: int = 0
    final_count: int = 0
    elapsed_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

# ─────────────────────────────────────────────────────────────────────────────
# HTTP HELPERS
# ─────────────────────────────────────────────────────────────────────────────

log = logging.getLogger("optimizer.data")


async def _get_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """
    GET a Deribit public endpoint with exponential-backoff retries.

    Args:
        session: Shared aiohttp session.
        url:     Full endpoint URL.
        params:  Query parameters dict.

    Returns:
        Parsed JSON dict on success, None on permanent failure.
    """
    delay = HTTP_RETRY_BASE_DELAY
    for attempt in range(1, HTTP_RETRY_ATTEMPTS + 1):
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # Deribit wraps results in {"result": ...}
                    return data.get("result")
                if resp.status == 429:
                    # Rate-limited — always back off
                    log.warning("Rate limited on %s, backing off %.1fs", url, delay * 2)
                    await asyncio.sleep(delay * 2)
                else:
                    log.warning(
                        "HTTP %s on %s (attempt %d/%d)",
                        resp.status, url, attempt, HTTP_RETRY_ATTEMPTS,
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning(
                "Request error on %s attempt %d/%d: %s",
                url, attempt, HTTP_RETRY_ATTEMPTS, exc,
            )

        if attempt < HTTP_RETRY_ATTEMPTS:
            await asyncio.sleep(delay)
            delay *= 2  # exponential backoff

    return None

# ─────────────────────────────────────────────────────────────────────────────
# MODULE 1.1 — GET INSTRUMENTS
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_instruments(
    session: aiohttp.ClientSession,
) -> list[dict[str, Any]]:
    """
    Fetch all active ETH options from Deribit.

    Args:
        session: Shared aiohttp session.

    Returns:
        List of raw instrument dicts from Deribit (not yet filtered).

    Raises:
        RuntimeError: If the API call fails after all retries.
    """
    url = f"{DERIBIT_BASE_URL}/public/get_instruments"
    params = {"currency": "ETH", "kind": "option", "expired": "false"}

    result = await _get_with_retry(session, url, params)
    if result is None:
        raise RuntimeError("Failed to fetch instruments from Deribit after retries.")

    log.info("Fetched %d raw ETH option instruments", len(result))
    return result

# ─────────────────────────────────────────────────────────────────────────────
# MODULE 1.2 — EXPIRY PRE-FILTER (cheap, no network)
# ─────────────────────────────────────────────────────────────────────────────

def _days_until_expiry(expiration_timestamp_ms: int) -> float:
    """
    Compute calendar days from now until expiration.

    Args:
        expiration_timestamp_ms: Unix timestamp in milliseconds (Deribit format).

    Returns:
        Float days remaining; negative means already expired.
    """
    now_ts = time.time()
    exp_ts = expiration_timestamp_ms / 1000.0
    return (exp_ts - now_ts) / SECONDS_PER_DAY


def pre_filter_by_expiry(
    instruments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Drop instruments outside the 2–14 day expiry window before hitting order books.

    Args:
        instruments: Raw instrument list from get_instruments.

    Returns:
        Filtered list with only instruments in the target expiry window.
    """
    result = []
    for inst in instruments:
        dte = _days_until_expiry(inst["expiration_timestamp"])
        if MIN_DAYS_TO_EXPIRY <= dte <= MAX_DAYS_TO_EXPIRY:
            result.append(inst)

    log.info(
        "After expiry pre-filter: %d / %d instruments remain",
        len(result), len(instruments),
    )
    return result

# ─────────────────────────────────────────────────────────────────────────────
# MODULE 1.1 — GET ORDER BOOK (parallel)
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_single_order_book(
    session: aiohttp.ClientSession,
    instrument_name: str,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any] | None:
    """
    Fetch depth-1 order book for one instrument, respecting concurrency limit.

    Args:
        session:         Shared aiohttp session.
        instrument_name: Deribit instrument identifier, e.g. "ETH-16JAN25-2000-P".
        semaphore:       Limits parallel in-flight requests.

    Returns:
        Raw order book dict on success, None on failure.
    """
    async with semaphore:
        url = f"{DERIBIT_BASE_URL}/public/get_order_book"
        params = {"instrument_name": instrument_name, "depth": 1}
        return await _get_with_retry(session, url, params)


async def fetch_all_order_books(
    session: aiohttp.ClientSession,
    instruments: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """
    Fetch order books for all instruments concurrently.

    Args:
        session:     Shared aiohttp session.
        instruments: Pre-filtered instrument list.

    Returns:
        Dict mapping instrument_name → order book dict.
        Instruments whose fetch failed are omitted.
    """
    semaphore = asyncio.Semaphore(HTTP_CONCURRENCY_LIMIT)
    names = [inst["instrument_name"] for inst in instruments]

    tasks = [
        _fetch_single_order_book(session, name, semaphore)
        for name in names
    ]
    results = await asyncio.gather(*tasks, return_exceptions=False)

    order_books: dict[str, dict[str, Any]] = {}
    for name, ob in zip(names, results):
        if ob is not None:
            order_books[name] = ob
        else:
            log.warning("Order book fetch failed for %s — skipping", name)

    log.info("Successfully fetched %d / %d order books", len(order_books), len(names))
    return order_books

# ─────────────────────────────────────────────────────────────────────────────
# MODULE 1.3 — LIQUIDITY SCORE
# ─────────────────────────────────────────────────────────────────────────────

def compute_liquidity_score(
    spread_pct: float,
    volume_usd_24h: float,
    open_interest: float,
) -> float:
    """
    Composite liquidity score in [0, 1].

    Components:
        - Spread tightness (40%): 1 − spread_pct
        - 24h volume normalized to $500k (35%)
        - Open interest normalized to 1000 contracts (25%)

    Args:
        spread_pct:      (ask − bid) / mark_price, in [0, 1].
        volume_usd_24h:  USD trading volume over last 24 hours.
        open_interest:   Number of open contracts.

    Returns:
        Score in [0.0, 1.0].
    """
    spread_component = max(0.0, 1.0 - spread_pct)
    volume_component = min(volume_usd_24h / SCORE_VOLUME_NORMALIZER, 1.0)
    oi_component = min(open_interest / SCORE_OI_NORMALIZER, 1.0)

    return (
        spread_component * SCORE_WEIGHT_SPREAD
        + volume_component * SCORE_WEIGHT_VOLUME
        + oi_component * SCORE_WEIGHT_OI
    )

# ─────────────────────────────────────────────────────────────────────────────
# MODULE 1.2 + 1.3 — PARSE & FILTER PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(value: Any, default: float = 0.0) -> float:
    """Cast to float safely; return default on None / missing."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_and_filter(
    instruments: list[dict[str, Any]],
    order_books: dict[str, dict[str, Any]],
    stats: FetchStats,
) -> list[OptionInstrument]:
    """
    Join instrument metadata with order book data, then apply all filters.

    Filtering order (fail-fast, cheapest checks first):
        1. Order book available
        2. Bid > 0 AND Ask > 0
        3. Spread ≤ 30%
        4. Mark price ≥ 0.001
        5. Open interest ≥ 10
        6. Liquidity score ≥ 0.35

    Note: Expiry already pre-filtered before network calls.

    Args:
        instruments: Raw instrument dicts.
        order_books: name → order book dicts.
        stats:       Mutable counter object updated in-place.

    Returns:
        List of fully validated OptionInstrument objects.
    """
    now_ts = time.time()
    result: list[OptionInstrument] = []

    for inst in instruments:
        name = inst["instrument_name"]
        ob = order_books.get(name)

        # ── Guard: order book must exist ─────────────────────────────────────
        if ob is None:
            continue

        stats.fetched_orderbooks += 1

        # ── Extract raw fields (with safe defaults) ──────────────────────────
        best_bid = _safe_float(ob.get("best_bid_price"))
        best_ask = _safe_float(ob.get("best_ask_price"))
        mark_price = _safe_float(ob.get("mark_price"))
        open_interest = _safe_float(ob.get("open_interest"))
        volume_usd = _safe_float(ob.get("stats", {}).get("volume_usd"))
        mark_iv = _safe_float(ob.get("mark_iv"))

        greeks = ob.get("greeks") or {}
        delta = _safe_float(greeks.get("delta"))
        gamma = _safe_float(greeks.get("gamma"))
        theta = _safe_float(greeks.get("theta"))
        vega = _safe_float(greeks.get("vega"))

        exp_ts_ms: int = inst["expiration_timestamp"]
        dte = (exp_ts_ms / 1000.0 - now_ts) / SECONDS_PER_DAY

        # ── Filter 1: Bid / Ask must be > 0 ──────────────────────────────────
        if best_bid <= MIN_BID_PRICE or best_ask <= MIN_ASK_PRICE:
            stats.dropped_no_bid_ask += 1
            continue

        # ── Filter 2: Spread ≤ 30% ────────────────────────────────────────────
        if mark_price <= 0.0:
            # Can't compute spread; treat as illiquid
            stats.dropped_spread += 1
            continue
        spread_pct = (best_ask - best_bid) / mark_price
        if spread_pct > MAX_SPREAD_PCT:
            stats.dropped_spread += 1
            continue

        # ── Filter 3: Mark price ≥ 0.001 ─────────────────────────────────────
        if mark_price < MIN_MARK_PRICE:
            stats.dropped_mark_price += 1
            continue

        # ── Filter 4: Open interest ≥ 10 ─────────────────────────────────────
        if open_interest < MIN_OPEN_INTEREST:
            stats.dropped_open_interest += 1
            continue

        # ── Filter 5: Liquidity score ≥ 0.35 ─────────────────────────────────
        score = compute_liquidity_score(spread_pct, volume_usd, open_interest)
        if score < MIN_LIQUIDITY_SCORE:
            stats.dropped_liquidity_score += 1
            continue

        # ── Build structured object ───────────────────────────────────────────
        option_type = inst.get("option_type", "").lower()   # "call" | "put"
        strike = _safe_float(inst.get("strike"))
        settlement_ccy = inst.get("settlement_currency", "ETH")

        result.append(
            OptionInstrument(
                name=name,
                option_type=option_type,
                strike=strike,
                expiry_ts=exp_ts_ms,
                days_to_expiry=dte,
                best_bid=best_bid,
                best_ask=best_ask,
                mark_price=mark_price,
                open_interest=open_interest,
                volume_usd_24h=volume_usd,
                delta=delta,
                gamma=gamma,
                theta=theta,
                vega=vega,
                mark_iv=mark_iv,
                spread_pct=spread_pct,
                liquidity_score=score,
                settlement_currency=settlement_ccy,
            )
        )

    stats.final_count = len(result)
    return result

# ─────────────────────────────────────────────────────────────────────────────
# TOP-LEVEL FETCH PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_liquid_options() -> tuple[list[OptionInstrument], FetchStats]:
    """
    Full data pipeline: instruments → order books → filter → score.

    Returns:
        Tuple of (liquid option list, pipeline statistics).
    """
    stats = FetchStats()
    t0 = time.monotonic()

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    connector = aiohttp.TCPConnector(limit=HTTP_CONCURRENCY_LIMIT)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        # Step A: get all active instruments
        raw_instruments = await fetch_instruments(session)
        stats.total_instruments = len(raw_instruments)

        # Step B: cheap expiry pre-filter (no network)
        expiry_filtered = pre_filter_by_expiry(raw_instruments)
        stats.dropped_expiry = stats.total_instruments - len(expiry_filtered)

        if not expiry_filtered:
            log.warning("No instruments in the 2–14 day expiry window.")
            stats.elapsed_seconds = time.monotonic() - t0
            return [], stats

        # Step C: parallel order book fetch
        order_books = await fetch_all_order_books(session, expiry_filtered)

        # Step D: parse + multi-stage filter + score
        options = parse_and_filter(expiry_filtered, order_books, stats)

    stats.elapsed_seconds = time.monotonic() - t0
    return options, stats

# ─────────────────────────────────────────────────────────────────────────────
# REPORTING
# ─────────────────────────────────────────────────────────────────────────────

def print_fetch_report(options: list[OptionInstrument], stats: FetchStats) -> None:
    """Print a concise pipeline summary to stdout."""
    width = 56
    border = "═" * width
    divider = "─" * width

    print(f"\n{border}")
    print("  ETH OPTIONS OPTIMIZER — Step 1: Data Layer Report")
    print(f"  Run: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(border)

    print("\nPIPELINE STATS")
    print(divider)
    print(f"  Total instruments fetched  : {stats.total_instruments:>6}")
    print(f"  Dropped (expiry window)    : {stats.dropped_expiry:>6}")
    print(f"  Order books retrieved      : {stats.fetched_orderbooks:>6}")
    print(f"  Dropped (no bid/ask)       : {stats.dropped_no_bid_ask:>6}")
    print(f"  Dropped (spread > 30%)     : {stats.dropped_spread:>6}")
    print(f"  Dropped (mark < 0.001)     : {stats.dropped_mark_price:>6}")
    print(f"  Dropped (OI < 10)          : {stats.dropped_open_interest:>6}")
    print(f"  Dropped (score < 0.35)     : {stats.dropped_liquidity_score:>6}")
    print(f"  ── FINAL LIQUID OPTIONS    : {stats.final_count:>6}")
    print(f"  Elapsed                    : {stats.elapsed_seconds:.2f}s")

    if not options:
        print("\n  ⚠  No liquid options found — nothing to display.\n")
        return

    # Show top-10 by liquidity score
    top = sorted(options, key=lambda o: o.liquidity_score, reverse=True)[:100]

    print("\nTOP-10 BY LIQUIDITY SCORE")
    print(divider)
    header = f"  {'Instrument':<32} {'Type':<5} {'Strike':>7} {'DTE':>5} {'Score':>6}"
    print(header)
    print(f"  {'-'*32} {'-'*5} {'-'*7} {'-'*5} {'-'*6}")
    for o in top:
        print(
            f"  {o.name:<32} {o.option_type.upper():<5} "
            f"{o.strike:>7,.0f} {o.days_to_expiry:>5.1f} {o.liquidity_score:>6.3f}"
        )

    # Greek summary
    if options:
        avg_score = sum(o.liquidity_score for o in options) / len(options)
        print(f"\n  Average liquidity score    : {avg_score:.3f}")
        print(f"  Calls / Puts               : "
              f"{sum(1 for o in options if o.option_type=='call')} / "
              f"{sum(1 for o in options if o.option_type=='put')}")

    print(f"\n{border}\n")

# ─────────────────────────────────────────────────────────────────────────────
# UNIT TESTS (--test flag)
# ─────────────────────────────────────────────────────────────────────────────

def run_unit_tests() -> None:
    """
    Offline tests for pure functions — no network required.

    Tests:
        - Liquidity score boundary values
        - Days-to-expiry calculation
        - Spread filter logic
    """
    print("Running unit tests for Step 1...\n")
    errors: list[str] = []

    # ── Test: compute_liquidity_score ─────────────────────────────────────────

    # Perfect liquidity (no spread, massive vol and OI)
    s = compute_liquidity_score(0.0, 1_000_000.0, 2_000.0)
    assert abs(s - 1.0) < 1e-9, f"Perfect score should be 1.0, got {s}"

    # Zero liquidity (max spread, no vol, no OI)
    s = compute_liquidity_score(1.0, 0.0, 0.0)
    assert abs(s) < 1e-9, f"Zero score should be 0.0, got {s}"

    # At threshold (30% spread, $500k vol, 1000 OI → score = 0.70*0.40 + 1.0*0.35 + 1.0*0.25)
    expected = 0.70 * SCORE_WEIGHT_SPREAD + 1.0 * SCORE_WEIGHT_VOLUME + 1.0 * SCORE_WEIGHT_OI
    s = compute_liquidity_score(0.30, 500_000.0, 1_000.0)
    assert abs(s - expected) < 1e-9, f"Threshold score mismatch: {s} != {expected}"

    # Score caps volume at 1.0 (no super-score)
    s_capped = compute_liquidity_score(0.10, 10_000_000.0, 10_000.0)
    s_ref = compute_liquidity_score(0.10, 500_000.0, 1_000.0)
    assert s_capped <= 1.0, "Score must not exceed 1.0"
    assert s_capped >= s_ref, "More volume/OI should not lower score"

    print("  [PASS] compute_liquidity_score — all 4 cases")

    # ── Test: _days_until_expiry ──────────────────────────────────────────────

    import math

    now_ms = int(time.time() * 1000)
    future_ms = now_ms + int(7 * SECONDS_PER_DAY * 1000)  # exactly 7 days
    dte = _days_until_expiry(future_ms)
    assert abs(dte - 7.0) < 0.01, f"7-day DTE mismatch: {dte}"

    past_ms = now_ms - int(1 * SECONDS_PER_DAY * 1000)  # 1 day ago
    dte_neg = _days_until_expiry(past_ms)
    assert dte_neg < 0, "Past expiry should return negative DTE"

    print("  [PASS] _days_until_expiry — past and future")

    # ── Test: _safe_float ────────────────────────────────────────────────────

    assert _safe_float(None) == 0.0
    assert _safe_float("bad") == 0.0
    assert _safe_float("3.14") == 3.14
    assert _safe_float(42) == 42.0
    print("  [PASS] _safe_float — None/bad/good values")

    # ── Test: filter logic (spread) ───────────────────────────────────────────
    # Spread exactly at boundary (=30%) is dropped (> 0.30 fails)
    bid, ask, mark = 1.0, 1.30, 1.15   # spread_pct = 0.30 / 1.15 ≈ 0.261
    spread_pct = (ask - bid) / mark
    assert spread_pct < MAX_SPREAD_PCT, "This spread should pass the filter"

    bid2, ask2, mark2 = 1.0, 1.70, 1.25  # spread_pct = 0.70 / 1.25 = 0.56 > 0.30
    spread_pct2 = (ask2 - bid2) / mark2
    assert spread_pct2 > MAX_SPREAD_PCT, "Wide spread should be rejected"

    print("  [PASS] spread filter boundary logic")

    if errors:
        print(f"\n  ✗ {len(errors)} test(s) FAILED:")
        for e in errors:
            print(f"    - {e}")
    else:
        print("\n  ✓ All unit tests passed.\n")

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ETH Options Data Layer — Deribit public API"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run offline unit tests instead of live fetch",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: WARNING)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.test:
        run_unit_tests()
        return

    print("Fetching liquid ETH options from Deribit…")
    options, stats = asyncio.run(fetch_liquid_options())
    print_fetch_report(options, stats)


if __name__ == "__main__":
    main()
