"""
ETH Options Optimizer — Final CLI
===================================
Production-grade optimizer that:
  1. Fetches liquid ETH options from Deribit (no auth)
  2. Builds payoff matrix and Greek vectors
  3. Solves MILP for maximum scenario-weighted theta
  4. Refines margins via Deribit API
  5. Prints formatted result to stdout

Usage:
    python optimizer.py                   # live Deribit data
    python optimizer.py --test            # synthetic data (no network)
    python optimizer.py --spot 2500       # override spot price
    python optimizer.py --verbose         # detailed output

Dependencies:
    pip install aiohttp cvxpy highspy numpy
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import numpy as np

from optimizer_step1 import (
    DERIBIT_BASE_URL,
    HTTP_TIMEOUT_SECONDS,
    HTTP_CONCURRENCY_LIMIT,
    OptionInstrument,
    FetchStats,
    fetch_liquid_options,
    _get_with_retry,
)
from optimizer_step2 import (
    PayoffPackage,
    build_payoff_package,
    build_holding_period_payoff_matrix,
)
from optimizer_step3 import (
    MILPParams,
    SolverStatus,
    _make_synthetic_instruments,
)
from optimizer_step4 import (
    ExtendedMILPParams,
    ExtendedMILPResult,
    MARKET_SCENARIOS,
    IV_STRESS_SHIFT,
    IV_STRESS_FLOOR_MULTIPLIER,
    solve_portfolio_v4,
)
from optimizer_step5 import (
    MarginRefinementResult,
    refine_margins,
)

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────

log = logging.getLogger("optimizer")

# ─────────────────────────────────────────────────────────────────────────────
# MODULE 6.1 — FETCH SPOT PRICE
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_spot_price() -> float:
    """
    Fetch current ETH/USD index price from Deribit.

    Uses the /public/get_index_price endpoint with index_name=eth_usd.

    Returns:
        ETH spot price in USD.

    Raises:
        RuntimeError: If API call fails after retries.
    """
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    connector = aiohttp.TCPConnector(limit=HTTP_CONCURRENCY_LIMIT)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        url = f"{DERIBIT_BASE_URL}/public/get_index_price"
        params = {"index_name": "eth_usd"}

        result = await _get_with_retry(session, url, params)
        if result is None:
            raise RuntimeError("Failed to fetch ETH spot price from Deribit.")

        price = result.get("index_price")
        if price is None:
            raise RuntimeError(f"Missing index_price in response: {result}")

        return float(price)


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 6.2 — FORMATTED OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

def _format_expiry_longform(expiry_ts: int) -> str:
    """Format Deribit expiry timestamp (ms) as '16 January 2025'."""
    if expiry_ts == 0:
        return "N/A"
    dt = datetime.utcfromtimestamp(expiry_ts / 1000)
    return f"{dt.day} {dt.strftime('%B %Y')}"


def _format_usd(val: float) -> str:
    """Format USD value with sign and comma separator."""
    if val >= 0:
        return f"+${val:>,.0f}"
    return f"-${abs(val):>,.0f}"


def print_final_report(
    result: ExtendedMILPResult,
    refinement: MarginRefinementResult,
    pkg: PayoffPackage,
    stats: Optional[FetchStats],
    spot_price: float,
    params: ExtendedMILPParams,
    spot_drift: Optional[dict] = None,
) -> None:
    """
    Print the full human-readable optimization report.

    Format matches the specification from the technical requirements.

    Args:
        result:      Final ExtendedMILPResult.
        refinement:  MarginRefinementResult from Step 5.
        pkg:         PayoffPackage (possibly updated by Step 5).
        stats:       FetchStats from Step 1 (None for synthetic data).
        spot_price:  ETH spot price used.
        params:      ExtendedMILPParams used.
        spot_drift:  Optional dict with spot drift info (elapsed_seconds,
                     spot_initial, spot_final, drift_pct, is_stale).
    """
    W = 60
    border  = "\u2550" * W
    divider = "\u2500" * W
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Use the possibly-updated package from refinement
    final_pkg = refinement.pkg if refinement.pkg is not None else pkg

    # ── HEADER ────────────────────────────────────────────────────────────────
    print(f"\n{border}")
    print(f" ETH OPTIONS OPTIMIZER \u2014 Deribit")
    print(f" Run: {now_str} | ETH Spot: ${spot_price:,.0f}")
    print(border)

    if result.status == SolverStatus.INFEASIBLE:
        print("\n  INFEASIBLE \u2014 no solution found.\n")
        for msg in result.diagnostics:
            print(f"  {msg}")
        if refinement.diagnostics:
            for msg in refinement.diagnostics:
                print(f"  {msg}")
        print(f"\n{border}\n")
        return

    if result.status == SolverStatus.ERROR or result.x is None:
        print("\n  ERROR \u2014 solver failed.\n")
        print(f"\n{border}\n")
        return

    p = result.params_used

    # ── OPTIMAL POSITION ──────────────────────────────────────────────────────
    solver_name = result.solver_used or "N/A"
    status_name = result.status.name
    if result.relaxation_step > 0:
        status_name += f" (relaxed {result.relaxation_step}/3)"

    n_legs = len(result.active_legs)
    print(f"\nOPTIMAL POSITION — {n_legs} Leg{'s' if n_legs != 1 else ''}  "
          f"[Solver: {solver_name} | Status: {status_name}]")
    print(divider)

    for i in result.active_legs:
        inst = final_pkg.instruments[i]
        xi   = result.x[i]

        action   = "BUY" if xi > 0 else "SELL"
        opt_type = inst.option_type.upper()
        expiry   = _format_expiry_longform(inst.expiry_ts)

        print(f"  {action} {opt_type} {inst.strike:,.0f} {expiry}  (qty {xi:+d})")

    if not result.active_legs:
        print("  (no active positions \u2014 zero solution)")

    # ── CONSTRAINT RELAXATION (Mod 2) ─────────────────────────────────────────
    if result.relaxation_step > 0 and result.params_used is not None:
        p_relaxed = result.params_used
        print(f"\nCONSTRAINT RELAXATION  (step {result.relaxation_step}/3 needed)")
        print(divider)
        # Step 1 always applies if relaxation_step >= 1
        print(f"  PNL_FLOOR:      ${params.pnl_floor:,.0f}  \u2192  ${p_relaxed.pnl_floor:,.0f}  (+20%)")
        if result.relaxation_step >= 2:
            print(f"  MARGIN_BUDGET:  ${params.margin_budget:,.0f}  \u2192  ${p_relaxed.margin_budget:,.0f}  (+20%)")
        if result.relaxation_step >= 3:
            print(f"  MAX_DELTA:       {params.max_delta:.3f}  \u2192  {p_relaxed.max_delta:.3f}  (\u00d72.0)")

    # ── NET GREEKS ────────────────────────────────────────────────────────────
    print(f"\nNET GREEKS")
    print(divider)

    holding = params.holding_days
    print(f"  Theta (USD/day):    ${result.expected_theta_usd:>10.2f}")
    print(f"  Theta x {holding}d total:  ${result.expected_theta_usd * holding:>10.2f}")

    # Fee reporting: fee_vec @ |x|
    total_fees = np.sum(final_pkg.greeks.fee_vec * np.abs(result.x))
    net_expected_profit = result.expected_theta_usd * holding - total_fees
    print(f"  Round-trip Fees:    ${total_fees:>10.2f}")
    print(f"  Net Profit (θ-fees): ${net_expected_profit:>8.2f}")
    print(f"  Delta:              {result.portfolio_delta:>11.4f}   (limit \u00b1{params.max_delta:.2f})")
    print(f"  Gamma:              {result.portfolio_gamma:>11.5f}   (limit \u2265{-params.max_gamma:.3f})")
    print(f"  Vega (USD/1%):     ${result.portfolio_vega:>10.2f}   (limit \u00b1${params.max_vega_usd:.0f})")

    # ── THETA SCENARIO BREAKDOWN ──────────────────────────────────────────────
    print(f"\nTHETA SCENARIOS")
    print(divider)
    print(f"  {'Scenario':<12} {'Spot':>8} {'IV shift':>9} {'Prob':>6} {'Theta/d':>10}")
    print(f"  {'-'*12} {'-'*8} {'-'*9} {'-'*6} {'-'*10}")

    for s, theta_s in zip(MARKET_SCENARIOS, result.theta_by_scenario):
        print(
            f"  {s['name']:<12} x{s['spot_mult']:.3f}   "
            f"{s['iv_shift']:>+.0%}     {s['prob']:.0%}    "
            f"${theta_s:>8.2f}"
        )

    divider_line = '\u2500' * 50
    print(f"  {divider_line}")
    print(f"  {'Expected (weighted)':>30}   ${result.expected_theta_usd:>8.2f}/d")
    print(f"  {'Deribit theta (point est)':>30}   ${result.portfolio_theta:>8.2f}/d")

    # ── P&L SCENARIOS ─────────────────────────────────────────────────────────
    print(f"\nP&L SCENARIOS")
    print(divider)

    if result.pnl_by_price is not None and p:
        display_prices = [
            1_400, 1_500, 1_600, 1_700, 1_800, 1_900, 2_000,
            2_100, 2_200, 2_300, 2_500, 2_700,
        ]
        for dp in display_prices:
            matches = np.where(np.abs(final_pkg.grid.prices - dp) < 0.01)[0]
            if len(matches) == 0:
                continue
            pnl_val = result.pnl_by_price[matches[0]]

            if abs(dp - p.range_high) < 0.01:
                floor = p.pnl_ceiling
                label = " (ceiling)"
            elif abs(dp - params.spot_price) < 0.01:
                floor = p.pnl_floor
                label = " (spot)"
            else:
                floor = p.pnl_floor
                label = ""

            ok = pnl_val >= floor - 0.01
            mark = "\u2713" if ok else "\u26a0"
            print(f"  ETH ${dp:>5,}:  ${pnl_val:>8,.0f}   {mark}{label}")

    # ── P&L AT EXIT DATE (Black-Scholes pricing, not intrinsic) ────────────
    print(f"\nP&L AT EXIT DATE (closing at day {params.holding_days})")
    print(divider)

    holding_matrices = build_holding_period_payoff_matrix(
        instruments=final_pkg.instruments,
        spot_price=spot_price,
        holding_days=params.holding_days,
        scenarios=MARKET_SCENARIOS,
    )

    # Entry cost in USD: pay ask for longs, receive bid for shorts
    # min(x,0) is negative for shorts, so bid * negative = subtract premium received
    x_vec = result.x
    cost_usd = float(
        (final_pkg.entry.ask_vec * np.maximum(x_vec, 0)
         + final_pkg.entry.bid_vec * np.minimum(x_vec, 0)).sum()
    ) * spot_price

    # P_hold @ x_vec gives portfolio value at EACH grid price → pick row for scenario spot
    portfolio_at_grid = {}  # scenario -> array of portfolio values per price
    for s in MARKET_SCENARIOS:
        P_hold = holding_matrices[s["name"]]
        portfolio_at_grid[s["name"]] = P_hold @ x_vec  # shape [M]

    print(f"  {'Scenario':<12} {'Spot':>8} {'Exit P&L':>10}")
    print(f"  {'-'*12} {'-'*8} {'-'*10}")
    for s in MARKET_SCENARIOS:
        scenario_spot = spot_price * s["spot_mult"]
        # Find closest grid price to scenario spot
        j_closest = int(np.argmin(np.abs(final_pkg.grid.prices - scenario_spot)))
        exit_value = float(portfolio_at_grid[s["name"]][j_closest])
        pnl_exit = exit_value - cost_usd - total_fees
        print(f"  {s['name']:<12} ${scenario_spot:>7,.0f} ${pnl_exit:>9,.0f}")

    print(f"\n  Note: Exit P&L uses BS pricing (time value preserved)")
    print(f"        Expiration P&L above is worst-case intrinsic value")

    # ── RISK METRICS ──────────────────────────────────────────────────────────
    print(f"\nRISK METRICS")
    print(divider)

    # Margin: use real margin if available, otherwise approximation
    margin_used = result.margin_used
    margin_source = "approx"
    if refinement.final_margin_real > 0:
        margin_used = refinement.final_margin_real
        margin_source = "API"
    margin_pct = margin_used / params.margin_budget * 100 if params.margin_budget else 0

    print(
        f"  Est. Margin Used:  ${margin_used:>7,.0f} / ${params.margin_budget:>,.0f}  "
        f"({margin_pct:.1f}%)  [{margin_source}]"
    )

    # IV Stress
    stress_floor = params.pnl_floor * IV_STRESS_FLOOR_MULTIPLIER
    stress_ok = result.iv_stress_pnl >= stress_floor - 0.01
    stress_mark = '\u2713' if stress_ok else '\u26a0'
    print(
        f"  IV Stress P&L:     ${result.iv_stress_pnl:>7,.0f}  "
        f"(IV +{IV_STRESS_SHIFT:.0%})  "
        f"{stress_mark}"
    )

    # Liquidity score
    if result.active_legs:
        avg_liq = np.mean([
            final_pkg.instruments[i].liquidity_score
            for i in result.active_legs
        ])
        print(f"  Liquidity Score:    {avg_liq:.2f} / 1.00")

    # Max loss
    if result.pnl_by_price is not None:
        max_loss   = float(result.pnl_by_price.min())
        max_profit = float(result.pnl_by_price.max())
        worst_idx  = int(np.argmin(result.pnl_by_price))
        worst_price = final_pkg.grid.prices[worst_idx]
        print(f"  Max Loss (modeled): ${max_loss:>7,.0f} at ${worst_price:,.0f}")
        print(f"  Max Profit:         ${max_profit:>7,.0f}")

    print(f"  Solve Time:         {result.solve_time:.2f}s")

    # Margin refinement info
    if refinement.iterations_used > 0:
        conv = "\u2713 converged" if refinement.margin_converged else "\u26a0 not converged"
        print(
            f"  Margin Refinement:  {refinement.iterations_used} iteration(s), {conv}"
        )

    # ── DIAGNOSTICS ───────────────────────────────────────────────────────────
    if result.diagnostics or refinement.diagnostics:
        print(f"\nDIAGNOSTICS")
        print(divider)
        for msg in result.diagnostics:
            print(f"  {msg}")
        for msg in refinement.diagnostics:
            print(f"  {msg}")

    # ── LIVE SPOT DRIFT WARNING (Mod 3) ──────────────────────────────────────
    if spot_drift is not None:
        elapsed = spot_drift["elapsed_seconds"]
        drift   = spot_drift["drift_pct"]
        if spot_drift["is_stale"]:
            print(f"\n\u26a0  POSITION MAY BE STALE")
            print(divider)
            print(f"  Solver took {elapsed:.1f}s")
            print(
                f"  Spot: ${spot_drift['spot_initial']:,.0f} \u2192 "
                f"${spot_drift['spot_final']:,.0f}  "
                f"({drift:+.1%} drift)"
            )
            print(f"  Recommendation: re-run optimizer")
        else:
            print(f"\n\u2713 Position valid  "
                  f"(solver {elapsed:.1f}s, spot drift {drift:+.1%})")

    # ── FOOTER ────────────────────────────────────────────────────────────────
    if stats:
        total    = stats.total_instruments
        filtered = total - stats.final_count
        used     = len(result.active_legs)
        print(
            f"\n  INSTRUMENTS ANALYZED: {total} | "
            f"FILTERED OUT: {filtered} | USED: {used}"
        )

    print(f"{border}\n")


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 6.3 — LIVE PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

async def run_live_pipeline(
    params: ExtendedMILPParams,
    spot_override: Optional[float] = None,
    verbose: bool = False,
) -> None:
    """
    Full end-to-end pipeline on live Deribit data.

    Pipeline:
        1. Fetch ETH spot price (unless overridden)
        2. Fetch liquid options (Step 1)
        3. Build payoff package (Step 2)
        4. Solve extended MILP (Step 4)
        5. Refine margins via API (Step 5)
        6. Print formatted report (Step 6)

    Args:
        params:        ExtendedMILPParams.
        spot_override: If provided, use this spot price instead of fetching.
        verbose:       Print progress messages.
    """
    t0 = time.monotonic()

    # ── Step 1a: Get spot price ──────────────────────────────────────────────
    if spot_override is not None:
        spot_price = spot_override
        if verbose:
            print(f"Using provided spot price: ${spot_price:,.2f}")
    else:
        if verbose:
            print("Fetching ETH spot price from Deribit...")
        spot_price = await fetch_spot_price()
        if verbose:
            print(f"ETH Spot: ${spot_price:,.2f}")

    # Update params with live spot
    params.spot_price = spot_price
    # If range_high was not explicitly set, use spot as the profit-zone center
    if params.range_high <= 0:
        params.range_high = spot_price

    # ── Step 1b: Fetch options ───────────────────────────────────────────────
    if verbose:
        print("Fetching liquid ETH options from Deribit...")
    options, stats = await fetch_liquid_options()

    if not options:
        print("No liquid options found. Check network or adjust filters.")
        return

    if verbose:
        print(
            f"Fetched {stats.final_count} liquid options "
            f"from {stats.total_instruments} total ({stats.elapsed_seconds:.1f}s)"
        )

    # ── Step 2: Build payoff package ─────────────────────────────────────────
    if verbose:
        print("Building payoff package...")
    pkg = build_payoff_package(options, spot_price)

    if verbose:
        print(
            f"  {pkg.n_instruments} instruments x "
            f"{pkg.n_price_points} price points"
        )

    # ── Step 4: Solve MILP ───────────────────────────────────────────────────
    if verbose:
        print(
            f"Solving Extended MILP "
            f"({pkg.n_instruments} instruments x {len(MARKET_SCENARIOS)} scenarios)..."
        )
    solver_start = time.monotonic()
    result = solve_portfolio_v4(pkg, params)
    solver_elapsed = time.monotonic() - solver_start

    if verbose:
        print(f"  Status: {result.status.name}, time: {result.solve_time:.2f}s")

    # ── Step 4b: Spot drift check (only if solver was slow) ──────────────────
    spot_drift: Optional[dict] = None
    if solver_elapsed > 15:
        spot_now = await fetch_spot_price()
        drift = abs(spot_now - spot_price) / spot_price
        spot_drift = {
            "elapsed_seconds": solver_elapsed,
            "spot_initial":    spot_price,
            "spot_final":      spot_now,
            "drift_pct":       (spot_now - spot_price) / spot_price,
            "is_stale":        drift > 0.03,
        }
        if verbose:
            print(f"  Spot drift: {spot_drift['drift_pct']:+.1%}")

    # ── Step 5: Margin refinement ────────────────────────────────────────────
    if verbose:
        print("Running margin refinement...")
    refinement = await refine_margins(pkg, params, result, spot_price)

    # Use the refined result
    final_result = refinement.final_result
    total_time = time.monotonic() - t0
    final_result.solve_time = total_time

    if verbose:
        print(
            f"Refinement: {refinement.iterations_used} iterations, "
            f"converged={refinement.margin_converged}"
        )
        print(f"Total pipeline time: {total_time:.1f}s")
        print()

    # ── Step 6: Print report ─────────────────────────────────────────────────
    final_pkg = refinement.pkg if refinement.pkg is not None else pkg
    print_final_report(
        result     = final_result,
        refinement = refinement,
        pkg        = final_pkg,
        stats      = stats,
        spot_price = spot_price,
        params     = params,
        spot_drift = spot_drift,
    )

    # ── Step 7: Launch P&L chart ─────────────────────────────────────────────
    from optimizer_chart import launch_chart
    launch_chart(result=final_result, pkg=final_pkg, params=params, spot_price=spot_price)


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 6.4 — SYNTHETIC TEST MODE
# ─────────────────────────────────────────────────────────────────────────────

def run_synthetic(
    params: ExtendedMILPParams,
    verbose: bool = False,
) -> None:
    """
    Run the optimizer with synthetic instruments (no network).

    Useful for testing the full pipeline without Deribit access.
    """
    spot_price = params.spot_price
    if params.range_high <= 0:
        params.range_high = spot_price
    if verbose:
        print(f"Synthetic mode: spot=${spot_price:,.0f}")

    instruments = _make_synthetic_instruments(spot_price)
    pkg = build_payoff_package(instruments, spot_price)

    if verbose:
        print(
            f"  {pkg.n_instruments} instruments x "
            f"{pkg.n_price_points} price points"
        )

    result = solve_portfolio_v4(pkg, params)

    # Skip margin refinement in synthetic mode (no API)
    refinement = MarginRefinementResult(
        final_result=result,
        pkg=pkg,
        diagnostics=["Synthetic mode: margin refinement skipped."],
    )

    print_final_report(
        result     = result,
        refinement = refinement,
        pkg        = pkg,
        stats      = None,
        spot_price = spot_price,
        params     = params,
    )

    from optimizer_chart import launch_chart
    launch_chart(result=result, pkg=pkg, params=params, spot_price=spot_price)


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 6.5 — UNIT TESTS
# ─────────────────────────────────────────────────────────────────────────────

def run_unit_tests(verbose: bool = False) -> None:
    """
    End-to-end tests with synthetic data (no network).

    Tests:
        1. Full pipeline produces OPTIMAL result
        2. Report prints without errors
        3. All constraints satisfied
    """
    print("Unit tests: Final Optimizer (synthetic)\n")
    passed = 0
    failed = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            if verbose:
                print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name}" + (f": {detail}" if detail else ""))

    SPOT = 2_000.0
    params = ExtendedMILPParams(
        spot_price      = SPOT,
        max_qty         = 500,
        margin_budget   = 4_000.0,
        pnl_floor       = -300.0,
        pnl_ceiling     = +100.0,
        range_high      = 2_200.0,
        holding_days    = 5,
        # Soft Greek limits
        soft_delta      = 0.30,
        soft_vega_usd   = 800.0,
        soft_gamma      = 0.020,
        # Legacy hard limits (deprecated, kept for compatibility)
        max_delta       = 0.30,
        max_vega_usd    = 800.0,
        max_gamma       = 0.020,
        # Target line parameters
        range_low       = SPOT - 200,
        target_loss     = -500.0,
        target_profit   = +500.0,
        # Objective weights (sum = 1.0)
        weight_theta     = 0.45,
        weight_deviation = 0.225,
        weight_greeks    = 0.075,
        weight_fees      = 0.20,
        weight_margin    = 0.05,
        # Hard floor
        hard_floor      = -2500.0,
    )

    instruments = _make_synthetic_instruments(SPOT)
    pkg = build_payoff_package(instruments, SPOT)

    # ── TEST 1: Full pipeline ─────────────────────────────────────────────────
    print("TEST 1: Full synthetic pipeline")
    result = solve_portfolio_v4(pkg, params)
    check("Status OPTIMAL", result.status == SolverStatus.OPTIMAL,
          f"got {result.status.name}")
    check("Has active legs", len(result.active_legs) > 0)
    check("Has theta", result.expected_theta_usd != 0)
    print()

    # ── TEST 2: Constraints satisfied ─────────────────────────────────────────
    print("TEST 2: Constraints verification")
    if result.status == SolverStatus.OPTIMAL and result.x is not None:
        x_f = result.x.astype(float)
        gr  = pkg.greeks

        check("|Delta| <= max_delta",
              abs(float(gr.delta_vec @ x_f)) <= params.max_delta + 1e-6)
        check("|Vega| <= max_vega",
              abs(float(gr.vega_usd @ x_f)) <= params.max_vega_usd + 1e-6)
        check("Gamma >= -max_gamma",
              float(gr.gamma_vec @ x_f) >= -params.max_gamma - 1e-6)
        check("Margin <= budget",
              result.margin_used <= params.margin_budget + 1e-6)
        check("|x| <= max_qty",
              all(abs(xi) <= params.max_qty for xi in result.x))

        if result.pnl_by_price is not None:
            check("Min P&L >= floor",
                  float(result.pnl_by_price.min()) >= params.pnl_floor - 1.0)
    print()

    # ── TEST 3: Report prints without errors ──────────────────────────────────
    print("TEST 3: Report generation")
    try:
        import io as _io
        buf = _io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buf

        refinement = MarginRefinementResult(final_result=result, pkg=pkg)
        print_final_report(result, refinement, pkg, None, SPOT, params)

        sys.stdout = old_stdout
        report = buf.getvalue()
        check("Report non-empty", len(report) > 100)
        check("Contains OPTIMAL", "OPTIMAL" in report)
        check("Contains P&L SCENARIOS", "P&L SCENARIOS" in report)
        check("Contains RISK METRICS", "RISK METRICS" in report)
        if verbose:
            print(report[:500] + "...")
    except Exception as exc:
        sys.stdout = old_stdout
        check("Report without errors", False, str(exc))
    print()

    # ── Summary ───────────────────────────────────────────────────────────────
    total = passed + failed
    print("-" * 50)
    if failed == 0:
        print(f"  All {total} tests passed.")
    else:
        print(f"  {failed} of {total} tests failed.")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def build_argparse() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "ETH Options Optimizer \u2014 maximize scenario-weighted theta "
            "under P&L, margin, and Greeks constraints. "
            "Uses Deribit public API (no auth required)."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Mode
    parser.add_argument(
        "--test", action="store_true",
        help="Run with synthetic data (no network)")
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print progress messages")

    # Market parameters
    parser.add_argument(
        "--spot", type=float, default=None,
        help="Override ETH spot price (USD). If omitted, fetched from Deribit.")

    # Optimization parameters
    parser.add_argument(
        "--floor", type=float, default=-1500.0,
        help="Minimum P&L floor at all price points (USD)")
    parser.add_argument(
        "--ceiling", type=float, default=-500.0,
        help="Minimum P&L at range_high price (USD). 0 = break-even at target.")
    parser.add_argument(
        "--margin", type=float, default=15_000.0,
        help="Margin budget (USD)")
    parser.add_argument(
        "--max-qty", type=int, default=1000,
        help="Max contracts per instrument")
    parser.add_argument(
        "--holding-days", type=int, default=5,
        help="Holding period for theta calculation (days)")

    # Target line parameters (NEW)
    parser.add_argument(
        "--range-low", type=float, default=None,
        help="Left edge of target P&L line (USD). Defaults to spot-200 if not set.")
    parser.add_argument(
        "--target-loss", type=float, default=-500.0,
        help="Target P&L at range_low (USD, negative = loss)")
    parser.add_argument(
        "--target-profit", type=float, default=+500.0,
        help="Target P&L at range_high (USD, positive = profit)")

    # Greeks limits (soft limits for penalty, not hard constraints)
    parser.add_argument(
        "--max-delta", type=float, default=0.50,
        help="(Deprecated) Soft limit for delta penalty")
    parser.add_argument(
        "--max-vega", type=float, default=2000.0,
        help="(Deprecated) Soft limit for vega penalty (USD/1%% IV)")
    parser.add_argument(
        "--max-gamma", type=float, default=0.050,
        help="(Deprecated) Soft limit for gamma penalty")
    parser.add_argument(
        "--range-high", type=float, default=None,
        help=(
            "Price at which P&L must reach the ceiling target (USD). "
            "Defaults to current ETH spot price if not set."
        ))

    # Objective function weights (NEW)
    parser.add_argument(
        "--weight-theta", type=float, default=0.45,
        help="Weight for theta in objective (default 0.45)")
    parser.add_argument(
        "--weight-deviation", type=float, default=0.225,
        help="Weight for deviation from target line (default 0.225)")
    parser.add_argument(
        "--weight-greeks", type=float, default=0.075,
        help="Weight for greek penalties (default 0.075)")
    parser.add_argument(
        "--weight-fees", type=float, default=0.20,
        help="Weight for fee minimization (default 0.20)")
    parser.add_argument(
        "--weight-margin", type=float, default=0.05,
        help="Weight for margin minimization (default 0.05)")

    # Hard floor (NEW)
    parser.add_argument(
        "--hard-floor", type=float, default=-2500.0,
        help="Absolute minimum P&L at any price (USD, hard constraint)")

    # System
    parser.add_argument(
        "--log-level", default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity")

    return parser


def main() -> None:
    """CLI entry point."""
    # Ensure UTF-8 output on Windows
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    parser = build_argparse()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Build params (spot + range_high will be finalized in live pipeline)
    params = ExtendedMILPParams(
        spot_price      = args.spot if args.spot else 2_000.0,
        max_qty         = args.max_qty,
        margin_budget   = args.margin,
        pnl_floor       = args.floor,
        pnl_ceiling     = args.ceiling,
        # 0.0 = sentinel: pipeline will replace with live spot
        range_high      = args.range_high if args.range_high else 0.0,
        holding_days    = args.holding_days,
        # Target line parameters (new)
        range_low       = args.range_low,
        target_loss     = args.target_loss,
        target_profit   = args.target_profit,
        # Objective weights
        weight_theta     = args.weight_theta,
        weight_deviation = args.weight_deviation,
        weight_greeks    = args.weight_greeks,
        weight_fees      = args.weight_fees,
        weight_margin    = args.weight_margin,
        # Soft Greek limits (for penalty, not hard constraints)
        soft_delta      = args.max_delta,
        soft_vega_usd   = args.max_vega,
        soft_gamma      = args.max_gamma,
        # Hard floor (new)
        hard_floor      = args.hard_floor,
    )

    if args.test:
        run_unit_tests(verbose=args.verbose)
        return

    if args.spot is None:
        # Live mode: fetch spot from API
        asyncio.run(run_live_pipeline(params, spot_override=None, verbose=args.verbose))
    else:
        # Live mode with overridden spot
        asyncio.run(run_live_pipeline(params, spot_override=args.spot, verbose=args.verbose))


if __name__ == "__main__":
    main()
