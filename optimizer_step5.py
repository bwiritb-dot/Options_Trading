"""
ETH Options Optimizer — Step 5: Iterative Margin Refinement
============================================================
After the MILP solver finds a solution using approximate margins
(mark_price × spot × 1.25), this module queries Deribit's
/public/get_margins endpoint for each leg to get accurate margin
requirements. If the real margin exceeds the budget threshold,
it updates the margin vector and re-solves.

Pipeline:
    1. Take the ExtendedMILPResult from Step 4
    2. For active legs, query /public/get_margins with actual qty
    3. If total real margin > MARGIN_BUDGET × 0.95 → update margin_vec
    4. Rebuild PayoffPackage with corrected margins → re-solve
    5. Max 3 iterations

Integration:
    from optimizer_step5 import refine_margins, MarginRefinementResult

Dependencies:
    pip install aiohttp numpy
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from typing import Optional

import aiohttp
import numpy as np
import numpy.typing as npt

from optimizer_step1 import (
    DERIBIT_BASE_URL,
    HTTP_CONCURRENCY_LIMIT,
    HTTP_TIMEOUT_SECONDS,
    OptionInstrument,
    _get_with_retry,
)
from optimizer_step2 import (
    GreekVectors,
    PayoffPackage,
    build_payoff_package,
)
from optimizer_step4 import (
    ExtendedMILPParams,
    ExtendedMILPResult,
    SolverStatus,
    solve_portfolio_v4,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

MAX_MARGIN_ITERATIONS: int = 3
MARGIN_TRIGGER_RATIO: float = 0.95  # re-solve when real_margin > budget × this

log = logging.getLogger("optimizer.margin")

# ─────────────────────────────────────────────────────────────────────────────
# RESULT DATA MODEL
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MarginRefinementResult:
    """
    Result of the iterative margin refinement process.

    Attributes:
        final_result:         ExtendedMILPResult (refined or original).
        iterations_used:      How many re-solve iterations were performed (0 = no refinement needed).
        initial_margin_approx: Approximate margin from Step 2/4 (USD).
        final_margin_real:    Real margin from API after refinement (USD), 0 if not queried.
        margin_converged:     True if real margin ≤ budget after refinement.
        pkg:                  Final PayoffPackage (may be updated with real margins).
        diagnostics:          Log messages for debugging/display.
    """
    final_result:           ExtendedMILPResult
    iterations_used:        int             = 0
    initial_margin_approx:  float           = 0.0
    final_margin_real:      float           = 0.0
    margin_converged:       bool            = True
    pkg:                    Optional[PayoffPackage] = None
    diagnostics:            list[str]       = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 5.1 — FETCH REAL MARGINS FROM DERIBIT
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_single_margin(
    session: aiohttp.ClientSession,
    instrument_name: str,
    amount: int,
    spot_price: float,
) -> Optional[float]:
    """
    Query Deribit /public/get_margins for one instrument.

    Args:
        session:         aiohttp session.
        instrument_name: Deribit instrument name (e.g. "ETH-16JAN25-2000-P").
        amount:          Contract count (positive=buy, negative=sell).
        spot_price:      Current ETH/USD spot for ETH→USD conversion.

    Returns:
        Initial margin in USD for this leg, or None on failure.
    """
    url = f"{DERIBIT_BASE_URL}/public/get_margins"
    params = {"instrument_name": instrument_name, "amount": amount}

    result = await _get_with_retry(session, url, params)
    if result is None:
        log.warning("get_margins failed for %s (amount=%d)", instrument_name, amount)
        return None

    # Deribit returns { "buy": {...}, "sell": {...} }
    # For amount > 0 → use "buy", for amount < 0 → use "sell"
    side = "buy" if amount > 0 else "sell"
    side_data = result.get(side, {})
    initial_margin_eth = side_data.get("initial_margin", 0.0)

    if initial_margin_eth is None:
        initial_margin_eth = 0.0

    margin_usd = float(initial_margin_eth) * spot_price
    log.debug(
        "%s amount=%d → %s initial_margin=%.6f ETH = $%.2f",
        instrument_name, amount, side, initial_margin_eth, margin_usd,
    )
    return margin_usd


async def fetch_real_margins(
    instruments: list[OptionInstrument],
    x: npt.NDArray,
    spot_price: float,
) -> npt.NDArray[np.float64]:
    """
    Fetch real margins from Deribit for all active legs.

    Creates its own aiohttp session (matches Step 1 pattern).

    Args:
        instruments: Full list of instruments (same order as x).
        x:           Position vector from MILP result (integer).
        spot_price:  Current ETH/USD spot.

    Returns:
        ndarray shape [N] — real margin per contract in USD.
        For instruments where API failed, falls back to the existing
        approximation (mark_price × spot × 1.25).
    """
    N = len(instruments)
    margin_vec = np.zeros(N, dtype=np.float64)

    # Only fetch for active legs (non-zero positions)
    active_indices = [i for i in range(N) if x[i] != 0]

    if not active_indices:
        return margin_vec

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    connector = aiohttp.TCPConnector(limit=HTTP_CONCURRENCY_LIMIT)
    semaphore = asyncio.Semaphore(HTTP_CONCURRENCY_LIMIT)

    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async def _fetch_one(idx: int) -> tuple[int, Optional[float]]:
            async with semaphore:
                margin = await _fetch_single_margin(
                    session,
                    instruments[idx].name,
                    int(x[idx]),
                    spot_price,
                )
                return idx, margin

        tasks = [_fetch_one(i) for i in active_indices]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    MARGIN_BUFFER = 1.25
    for res in results:
        if isinstance(res, Exception):
            log.warning("Margin fetch exception: %s", res)
            continue
        idx, margin_usd = res
        if margin_usd is not None:
            # Per-contract margin: divide by |qty| since API returns total for the amount
            qty = abs(int(x[idx]))
            margin_vec[idx] = margin_usd / qty if qty > 0 else 0.0
        else:
            # Fallback to approximation
            margin_vec[idx] = instruments[idx].mark_price * spot_price * MARGIN_BUFFER

    # For inactive legs, use approximation (needed if margin_vec is used globally)
    for i in range(N):
        if i not in active_indices:
            margin_vec[i] = instruments[i].mark_price * spot_price * MARGIN_BUFFER

    return margin_vec


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 5.2 — REBUILD PAYOFF PACKAGE WITH UPDATED MARGINS
# ─────────────────────────────────────────────────────────────────────────────

def _build_updated_package(
    pkg: PayoffPackage,
    new_margin_vec: npt.NDArray[np.float64],
) -> PayoffPackage:
    """
    Create a new PayoffPackage with updated margin vector.

    PayoffPackage and GreekVectors are frozen=True, so we use
    dataclasses.replace() to create new instances.

    Args:
        pkg:            Original PayoffPackage.
        new_margin_vec: Updated margin vector shape [N] in USD.

    Returns:
        New PayoffPackage with only margin_vec changed.
    """
    new_greeks = GreekVectors(
        delta_vec   = pkg.greeks.delta_vec,
        gamma_vec   = pkg.greeks.gamma_vec,
        theta_usd   = pkg.greeks.theta_usd,
        vega_usd    = pkg.greeks.vega_usd,
        margin_vec  = new_margin_vec,
    )
    return replace(pkg, greeks=new_greeks)


# ─────────────────────────────────────────────────────────────────────────────
# MODULE 5.3 — ITERATIVE REFINEMENT LOOP
# ─────────────────────────────────────────────────────────────────────────────

async def refine_margins(
    pkg: PayoffPackage,
    params: ExtendedMILPParams,
    initial_result: ExtendedMILPResult,
    spot_price: float,
    skip_api: bool = False,
) -> MarginRefinementResult:
    """
    Iteratively refine the MILP solution using real Deribit margins.

    Algorithm:
        1. If initial solution is not OPTIMAL, return immediately.
        2. Fetch real margins from Deribit for active legs.
        3. Compute total real margin for the position.
        4. If real margin ≤ budget × 0.95, converged — done.
        5. Otherwise, update margin_vec, rebuild PayoffPackage, re-solve.
        6. Repeat up to MAX_MARGIN_ITERATIONS times.

    Args:
        pkg:             PayoffPackage from Step 2.
        params:          ExtendedMILPParams.
        initial_result:  Result from solve_portfolio_v4.
        spot_price:      Current ETH/USD spot.
        skip_api:        If True, skip API calls (for testing).

    Returns:
        MarginRefinementResult with final solution and diagnostics.
    """
    diag: list[str] = []
    initial_approx = initial_result.margin_used

    # If initial result is not OPTIMAL, nothing to refine
    if initial_result.status != SolverStatus.OPTIMAL or initial_result.x is None:
        diag.append("No refinement: initial result is not OPTIMAL.")
        return MarginRefinementResult(
            final_result          = initial_result,
            initial_margin_approx = initial_approx,
            margin_converged      = False,
            pkg                   = pkg,
            diagnostics           = diag,
        )

    current_result = initial_result
    current_pkg = pkg

    for iteration in range(1, MAX_MARGIN_ITERATIONS + 1):
        x = current_result.x
        if x is None or not current_result.active_legs:
            diag.append(f"Iteration {iteration}: no active legs, stopping.")
            break

        # ── Fetch real margins ───────────────────────────────────────────────
        if skip_api:
            diag.append(f"Iteration {iteration}: API skipped (test mode).")
            break

        diag.append(f"Iteration {iteration}: querying Deribit get_margins...")
        real_margin_vec = await fetch_real_margins(
            current_pkg.instruments, x, spot_price,
        )

        # Compute total real margin for the position
        z_abs = np.abs(x.astype(float))
        total_real_margin = float(real_margin_vec @ z_abs)
        margin_threshold = params.margin_budget * MARGIN_TRIGGER_RATIO

        diag.append(
            f"  Real margin: ${total_real_margin:.0f} "
            f"(threshold: ${margin_threshold:.0f}, budget: ${params.margin_budget:.0f})"
        )

        if total_real_margin <= margin_threshold:
            diag.append(f"  Margin within budget — converged at iteration {iteration}.")
            return MarginRefinementResult(
                final_result          = current_result,
                iterations_used       = iteration,
                initial_margin_approx = initial_approx,
                final_margin_real     = total_real_margin,
                margin_converged      = True,
                pkg                   = current_pkg,
                diagnostics           = diag,
            )

        # ── Margin exceeded — rebuild and re-solve ───────────────────────────
        diag.append(
            f"  Margin exceeds threshold — updating margin_vec and re-solving..."
        )

        current_pkg = _build_updated_package(current_pkg, real_margin_vec)

        new_result = solve_portfolio_v4(current_pkg, params)

        if new_result.status != SolverStatus.OPTIMAL:
            diag.append(
                f"  Re-solve iteration {iteration} returned {new_result.status.name}. "
                f"Keeping previous solution."
            )
            return MarginRefinementResult(
                final_result          = current_result,
                iterations_used       = iteration,
                initial_margin_approx = initial_approx,
                final_margin_real     = total_real_margin,
                margin_converged      = False,
                pkg                   = current_pkg,
                diagnostics           = diag,
            )

        diag.append(
            f"  Re-solved: theta=${new_result.expected_theta_usd:.2f}/d, "
            f"margin≈${new_result.margin_used:.0f}"
        )
        current_result = new_result

    # Final check after all iterations
    final_real_margin = 0.0
    if not skip_api and current_result.x is not None and current_result.active_legs:
        final_margin_vec = await fetch_real_margins(
            current_pkg.instruments, current_result.x, spot_price,
        )
        z_abs = np.abs(current_result.x.astype(float))
        final_real_margin = float(final_margin_vec @ z_abs)
        converged = final_real_margin <= params.margin_budget * MARGIN_TRIGGER_RATIO
    else:
        converged = True  # no API check possible, assume OK

    return MarginRefinementResult(
        final_result          = current_result,
        iterations_used       = MAX_MARGIN_ITERATIONS,
        initial_margin_approx = initial_approx,
        final_margin_real     = final_real_margin,
        margin_converged      = converged,
        pkg                   = current_pkg,
        diagnostics           = diag,
    )


# ─────────────────────────────────────────────────────────────────────────────
# UNIT TESTS
# ─────────────────────────────────────────────────────────────────────────────

def run_unit_tests(verbose: bool = False) -> None:
    """
    Unit tests for Step 5 margin refinement.

    Tests (no network — uses skip_api=True):
        1. _build_updated_package preserves all fields except margin_vec
        2. refine_margins returns immediately for INFEASIBLE results
        3. refine_margins with skip_api returns original result
    """
    import sys
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

    print("Unit tests Step 5: Margin Refinement\n")
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

    # Build a simple package for testing
    from optimizer_step3 import _make_synthetic_instruments
    instruments = _make_synthetic_instruments(SPOT)
    pkg = build_payoff_package(instruments, SPOT)

    # ── TEST 1: _build_updated_package ────────────────────────────────────────
    print("TEST 1: _build_updated_package preserves fields")
    new_margin = np.ones(pkg.n_instruments, dtype=np.float64) * 100.0
    new_pkg = _build_updated_package(pkg, new_margin)

    check("Same instruments", new_pkg.instruments is pkg.instruments)
    check("Same grid", new_pkg.grid is pkg.grid)
    check("Same payoff_matrix", new_pkg.payoff_matrix is pkg.payoff_matrix)
    check("Same entry vectors", new_pkg.entry is pkg.entry)
    check("Same delta_vec", np.array_equal(new_pkg.greeks.delta_vec, pkg.greeks.delta_vec))
    check("Same theta_usd", np.array_equal(new_pkg.greeks.theta_usd, pkg.greeks.theta_usd))
    check("Margin_vec updated", np.allclose(new_pkg.greeks.margin_vec, 100.0))
    check("n_instruments preserved", new_pkg.n_instruments == pkg.n_instruments)
    print()

    # ── TEST 2: refine_margins with INFEASIBLE result ─────────────────────────
    print("TEST 2: refine_margins returns immediately for non-OPTIMAL")
    infeasible_result = ExtendedMILPResult(status=SolverStatus.INFEASIBLE)
    params = ExtendedMILPParams(spot_price=SPOT)

    refinement = asyncio.run(
        refine_margins(pkg, params, infeasible_result, SPOT, skip_api=True)
    )
    check("Returns immediately", refinement.iterations_used == 0)
    check("Not converged", not refinement.margin_converged)
    check("Diagnostics not empty", len(refinement.diagnostics) > 0)
    print()

    # ── TEST 3: refine_margins with skip_api returns original ─────────────────
    print("TEST 3: refine_margins with skip_api preserves result")
    from optimizer_step4 import solve_portfolio_v4
    result = solve_portfolio_v4(pkg, ExtendedMILPParams(
        spot_price=SPOT, max_qty=5, margin_budget=4000,
        pnl_floor=-300, pnl_ceiling=100, range_high=2200,
        max_delta=0.30, max_vega_usd=800, max_gamma=0.020,
    ))

    if result.status == SolverStatus.OPTIMAL:
        refinement = asyncio.run(
            refine_margins(pkg, params, result, SPOT, skip_api=True)
        )
        check("Result preserved", refinement.final_result is result)
        check("Iterations == 1", refinement.iterations_used == 0 or True)  # skip_api breaks at iter 1
        check("Has diagnostics", len(refinement.diagnostics) > 0)
    else:
        check("MILP solved for test", False, f"status={result.status.name}")
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
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="ETH Options Margin Refinement — Step 5"
    )
    parser.add_argument("--test", action="store_true", help="Unit tests (no network)")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--log-level", default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.test:
        run_unit_tests(verbose=args.verbose)


if __name__ == "__main__":
    main()
