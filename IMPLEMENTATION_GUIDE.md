# ETH Options Optimizer — Complete Implementation Guide

**Last Updated:** 2026-04-03  
**Status:** Ready for implementation  
**Priority Order:** Requirements A & B, then Issues 1-6 in order  
**Added:** Volatility change modeling, exit date parameter, configurable parameters

---

## CRITICAL NEW REQUIREMENTS (User Feedback — Added 2026-04-03)

### Requirement A: Expected Volatility Change Modeling
**What:** Currently, scenarios include IV shifts (e.g., IV+0.08 in "crash"), but P&L calculation doesn't fully leverage expected vol changes as a primary driver.  
**Why:** Volatility changes are as important as spot moves for option P&L. A 5% spot move with +5% IV change can have very different profit outcomes than the same spot move with -5% IV change.  
**Implementation approach:**
- In `MARKET_SCENARIOS` (optimizer_step4.py:86-92), treat `iv_shift` as **expected volatility change**, not just scenario shock
- When building holding-period P&L matrix, use scenario-shifted IV explicitly
- Include a volatility forecast or base case in the reporting
- Show P&L decomposition: delta P&L + theta P&L + vega P&L for each scenario

### Requirement B: Exit Date (Hard Close Date)
**What:** Add `--exit-date` CLI parameter. On this date, the position MUST be closed, regardless of expiration dates of individual options. P&L is calculated at the exit date, not at individual option expirations.  
**Why:** Real trading doesn't hold to expiration. You need to know: "If I close everything on 2026-04-08, will I have made my target profit?"  
**Implementation approach:**
- Add `--exit-date` parameter to `optimizer.py` CLI (required, no default)
- Exit date must be >= today and <= max option DTE
- All P&L calculations shift from **expiration intrinsic** to **exit date market value**
- Holding period = exit_date - today (not DTE - holding_days)
- Theta calculation: only count theta days from today until exit_date
- Scenario P&L: use Black-Scholes pricing at (spot_scenario, K, T=time_to_exit_date, IV_scenario)

---

## REQUIREMENT C: Fully Configurable Parameters
**What:** User wants to control these via CLI/config instead of hardcoding:
1. **Range** — min/max spot multipliers for scenarios (e.g., 0.85x to 1.15x)
2. **Profit target** — max profit assumed at top of range
3. **Risk/loss floor** — maximum loss allowed at bottom of range
4. **Theta weighting** — how to weight closer vs farther expiration dates
5. **Expected volatility by day** — daily vol forecast from today until exit date
6. **Exit date** — already added (see Requirement B)
7. **Max quantity** — already a CLI parameter (will increase to 100)

**Implementation approach:**
- Add new `MarketConfig` dataclass in step4 with:
  ```python
  @dataclass
  class MarketConfig:
      spot_range_min: float      # 0.85 (min spot multiplier)
      spot_range_max: float      # 1.15 (max spot multiplier)
      profit_target_usd: float   # Expected profit at spot_range_max
      loss_floor_usd: float      # Max loss at spot_range_min
      theta_weight_curve: str    # "flat" | "linear" | "exponential" (closer dates weighted more)
      volatility_forecast: dict  # {day: vol_pct, ...} for each day to exit
  ```
- Add CLI parameters: `--spot-range-min`, `--spot-range-max`, `--profit-target`, `--loss-floor`, `--vol-forecast-file`
- Store in JSON config file that user can edit and pass via `--config config.json`
- Use these to dynamically build scenarios instead of fixed MARKET_SCENARIOS
- Adjust theta objective to use vol forecast for each day

**Priority:** Medium (Phase 2 after Issues 1-5 complete)  
**Impact:** Makes optimizer fully user-configurable for different market views

---

## ORIGINAL PLAN (5 Issues + Implementation)

### Issue 1: HTTP 400 on get_margins — **FIX FIRST** (Trivial)

**File:** `optimizer_step5.py:113`  
**Current code:**
```python
params = {"instrument_name": instrument_name, "amount": int(x[idx])}
```

**Fixed code:**
```python
params = {"instrument_name": instrument_name, "amount": abs(int(x[idx]))}
```

**Why:** Deribit `/public/get_margins` API requires `amount > 0`. For short positions (negative x), pass absolute value. Line 122 already selects the correct margin side (buy/sell) based on sign, so this is safe.

**Test:** Run `python optimizer.py --test` and verify no HTTP 400 warnings.

---

### Issue 2: Fee/Commission Modeling — **IMPLEMENT SECOND** (Critical Impact)

**Step 2a: Add fee calculation to GreekVectors (optimizer_step2.py)**

**Location:** Around line 368-379 where GreekVectors dataclass is defined  
**Add new field:**
```python
@dataclass
class GreekVectors:
    delta_vec: np.ndarray      # existing
    gamma_vec: np.ndarray      # existing
    theta_usd: np.ndarray      # existing
    vega_usd: np.ndarray       # existing
    margin_vec: np.ndarray     # existing
    fee_vec: np.ndarray        # NEW: per-contract round-trip fee in USD
```

**Step 2b: Calculate fee_vec in _build_greek_vectors() (optimizer_step2.py)**

**Location:** After line 420 where margin_vec is calculated  
**Add:**
```python
# Fee calculation: Deribit charges 0.03% of underlying per contract, capped at 12.5% of option price
# fee_vec stores the round-trip fee (entry + exit) per contract in USD
fee_vec = np.zeros(len(instruments))
for i, instr in enumerate(instruments):
    contract_fee_usd = min(
        0.0003 * spot_price,           # 0.03% of spot
        0.125 * instr.mark_price * spot_price  # 12.5% of option price
    )
    fee_vec[i] = 2.0 * contract_fee_usd  # Round-trip: entry + exit
```

**Step 2c: Return fee_vec from _build_greek_vectors()**

**Location:** Line 452 (end of function, current return statement)  
**Change from:**
```python
return GreekVectors(delta_vec, gamma_vec, theta_usd, vega_usd, margin_vec)
```

**To:**
```python
return GreekVectors(delta_vec, gamma_vec, theta_usd, vega_usd, margin_vec, fee_vec)
```

**Step 2d: Integrate fees into MILP objective (optimizer_step4.py)**

**Location:** Lines 576-583 where objective is formulated  
**Change from:**
```python
expected_theta_expr = sum(
    prob * (theta_s @ x)
    for prob, theta_s in zip(...)
)
objective = cp.Maximize(expected_theta_expr * params.holding_days)
```

**To:**
```python
expected_theta_expr = sum(
    prob * (theta_s @ x)
    for prob, theta_s in zip(...)
)
# Subtract round-trip fees from objective: fees scale with position size (abs value)
abs_x = cp.abs(x)
total_fees_expr = greeks.fee_vec @ abs_x

objective = cp.Maximize(expected_theta_expr * params.holding_days - total_fees_expr)
```

**Step 2e: Update P&L constraints to include fees (optimizer_step4.py)**

**Location:** Lines 600-605 where P&L constraints are added  
**Change from:**
```python
for j, prob in enumerate(probs):
    P_pnl = cp.sum(P[j] @ x)  # Expiration payoff
    constraint_expr = P_pnl - cost_usd_expr
    prob.add_constraints([constraint_expr >= pnl_floor])
```

**To:**
```python
for j, prob in enumerate(probs):
    P_pnl = cp.sum(P[j] @ x)  # Expiration payoff
    total_fees_expr = greeks.fee_vec @ cp.abs(x)
    constraint_expr = P_pnl - cost_usd_expr - total_fees_expr
    prob.add_constraints([constraint_expr >= pnl_floor])
```

**Step 2f: Add fee line to reporting (optimizer.py)**

**Location:** Lines 160-180 in print_final_report() where costs are displayed  
**Add after the "Entry Cost" section:**
```python
# Calculate total round-trip fees
total_fees = np.sum(greeks.fee_vec * np.abs(solution_x))
print(f"  Round-trip Fees (0.03% per contract):  ${total_fees:,.2f}")
print(f"  Net Expected Profit (theta - fees):    ${expected_theta - total_fees:,.2f}")
```

**Verify:** After running with fees, theta should be roughly 55% lower (from ~$723 to ~$350 net profit).

---

### Issue 3: Increase max_qty and Configure Solver — **IMPLEMENT THIRD** (Medium Priority)

**Step 3a: Increase max_qty default (optimizer.py)**

**Location:** Line 689  
**Change from:**
```python
"--max-qty", type=int, default=10, help="Maximum quantity per leg"
```

**To:**
```python
"--max-qty", type=int, default=100, help="Maximum quantity per leg"
```

**Step 3b: Switch primary solver to HiGHS (optimizer_step3.py)**

**Location:** Lines 140-160 where solver is selected  
**Change from:**
```python
problem.solve(solver=cp.GUROBI, verbose=False, ...)
```

**To:**
```python
problem.solve(
    solver=cp.HIGHS,
    verbose=False,
    mip_rel_gap=0.02,  # Accept 2% suboptimality
    time_limit=300
)
```

**Why HiGHS?** Open-source, handles MIP gaps well, scales with max_qty=100.

**Verify:** Run with `--max-qty 100` and verify solution converges in <300s.

---

### Issue 4: Scenario Reweighting (9 Scenarios) — **IMPLEMENT FOURTH** (Medium Priority)

**Location:** optimizer_step4.py, lines 86-92  
**Replace the 5-scenario definition with 9-scenario definition:**

```python
MARKET_SCENARIOS = [
    {"name": "crash",      "spot_mult": 0.85,  "iv_shift": +0.12, "prob": 0.03},
    {"name": "big_dip",    "spot_mult": 0.90,  "iv_shift": +0.08, "prob": 0.07},
    {"name": "dip",        "spot_mult": 0.95,  "iv_shift": +0.03, "prob": 0.12},
    {"name": "soft_dip",   "spot_mult": 0.975, "iv_shift": +0.01, "prob": 0.13},
    {"name": "base",       "spot_mult": 1.00,  "iv_shift":  0.00, "prob": 0.30},
    {"name": "soft_rally", "spot_mult": 1.025, "iv_shift": -0.01, "prob": 0.13},
    {"name": "rally",      "spot_mult": 1.05,  "iv_shift": -0.02, "prob": 0.12},
    {"name": "big_rally",  "spot_mult": 1.10,  "iv_shift": -0.04, "prob": 0.07},
    {"name": "surge",      "spot_mult": 1.15,  "iv_shift": -0.06, "prob": 0.03},
]
```

**Why:** Increases weight near spot (+-2.5%) from 40% to 56%, preserves tails at 10% each, uses log-normal probability distribution.

**Verify:** Check that theta table in output now shows 9 scenarios instead of 5.

---

### Issue 5: Holding-Period P&L — **IMPLEMENT FIFTH** (Higher Priority)

#### Phase 1: Reporting Only (Low Risk)

**Step 5a: Build holding-period payoff matrix (optimizer_step2.py)**

**Location:** New function after _build_payoff_matrix(), around line 230  
**Add new function:**

```python
def _build_holding_period_payoff_matrix(
    instruments: list[OptionInstrument],
    spot_price: float,
    holding_days: int,
    rate: float = 0.05
) -> np.ndarray:
    """
    Build payoff matrix using Black-Scholes pricing at exit date.
    P_hold[j, i] = BS_price(S_j, K_i, T_remaining, IV, r)
    where T_remaining = (instrument.dte - holding_days) / 365
    """
    n_scenarios = len(spot_scenarios)
    n_instruments = len(instruments)
    P_hold = np.zeros((n_scenarios, n_instruments))
    
    for j, spot_mult in enumerate(spot_scenarios):  # spot_scenarios from MARKET_SCENARIOS
        S_scenario = spot_price * spot_mult
        for i, instr in enumerate(instruments):
            # Days remaining at exit
            days_remaining = max(0, instr.dte - holding_days)
            T = days_remaining / 365.0
            
            if T <= 0:  # Expired or past exit date
                # Use intrinsic value (fallback to expiration payoff)
                if instr.is_call:
                    P_hold[j, i] = max(S_scenario - instr.strike, 0.0)
                else:
                    P_hold[j, i] = max(instr.strike - S_scenario, 0.0)
            else:
                # Use Black-Scholes pricing
                # iv_scenario = instr.iv + iv_shift (from scenario)
                # This requires passing scenario IV shifts to this function
                bs_price = bs_call_price(
                    S=S_scenario,
                    K=instr.strike,
                    T=T,
                    r=rate,
                    sigma=instr.iv + scenario_iv_shift  # Need to pass this
                ) if instr.is_call else bs_put_price(...)
                P_hold[j, i] = bs_price
    
    return P_hold
```

**Step 5b: Modify print_final_report() to show both P&L tables (optimizer.py)**

**Location:** Lines 200-250 where P&L scenarios are printed  
**Change to show two tables:**

```python
print("\n=== P&L AT EXPIRATION ===")
# [existing expiration payoff table]

print("\n=== P&L AT EXIT DATE ===")
# [new holding-period payoff table from P_hold matrix]
# For each scenario: show (S, IV shift, prob) and P&L at exit date

print("\n=== COMPARISON ===")
# For each scenario, show side-by-side: expiration vs exit date P&L difference
```

**Verify:** After running, both tables should appear. Exit date P&L should be non-zero even for OTM options (they still have time value).

#### Phase 2: Replace Objective (Medium Risk) — DEFER TO NEXT SESSION

This phase changes the optimization target from theta maximization to holding-period P&L maximization. **Do NOT implement until fees (Issue 2) are working correctly.**

---

## IMPLEMENTATION SEQUENCE (STRICT ORDER)

### Phase 1: Core Fixes (Days 1-2)
1. **Issue 1:** Fix HTTP 400 (1 line) → Verify margin refinement converges
2. **Issue 2:** Add fee modeling (6 changes) → Test that theta reduced by ~55%
3. **Issue 3:** Increase max_qty=100 + HiGHS solver → Verify solver completes in <300s
4. **Issue 4:** Expand to 9 scenarios → Check theta table shows 9 rows
5. **Issue 5 Phase 1:** Holding-period P&L reporting → Both P&L tables appear

### Phase 2: Requirements (Days 2-3)
6. **Requirement A:** Volatility change decomposition → P&L shows delta/theta/vega breakdown per scenario
7. **Requirement B:** Exit date parameter → All P&L calcs use exit date, not expiration
8. **Requirement C:** Configurable parameters → Accept MarketConfig from JSON/CLI

### Defer to Later Sessions
- Issue 5 Phase 2 (replace objective with holding-period P&L optimization)

---

## CLI CHANGES

### Current CLI (existing):
```
python optimizer.py --max-qty 10 --holding-days 5 --margin-budget 15000 --pnl-floor 500 --test
```

### New CLI (after Requirement B):
```
python optimizer.py \
  --max-qty 100 \
  --holding-days 5 \
  --exit-date 2026-04-08 \
  --margin-budget 15000 \
  --pnl-floor 500 \
  --test
```

**New parameter:**
- `--exit-date`: Date on which position is closed and P&L is calculated (format: YYYY-MM-DD, required)

**Implementation location:** `optimizer.py` lines 689-710 (argparse section)

---

## FILE CHANGE SUMMARY

| File | Changes | Lines | Priority |
|------|---------|-------|----------|
| optimizer_step5.py | Fix HTTP 400 (abs) | 113 | Issue 1 |
| optimizer_step2.py | Add fee_vec to GreekVectors | 368-379, 420-427, 452 | Issue 2 |
| optimizer_step4.py | Add fees to objective + constraints + scenarios | 86-92, 576-583, 600-605 | Issue 2, 4 |
| optimizer_step3.py | Switch to HiGHS solver | 140-160 | Issue 3 |
| optimizer.py | Increase max_qty default + add --exit-date param | 689, 700-710 | Issue 3, Req B |
| All steps | Import exit_date and use for P&L timing | Throughout | Req B |

---

## TESTING CHECKLIST

After each implementation step:

- [ ] Run `python optimizer.py --test` (existing tests pass)
- [ ] Run `python optimizer.py --verbose` and check output for:
  - [ ] No HTTP 400 warnings
  - [ ] Fee line showing total round-trip cost
  - [ ] Fewer legs with larger quantities
  - [ ] 9 scenarios in theta table (after step 4)
  - [ ] Both expiration and holding P&L tables (after step 5)
- [ ] Compare theta-after-fees vs pre-fees to confirm impact (~55% reduction)
- [ ] Verify margin refinement converges in 1-2 iterations (Issue 1)

---

## VERIFICATION EXAMPLES

### Fee Impact (Issue 2):
**Before:** Expected theta = $722.91, net profit = $722.91  
**After:** Expected theta = $722.91, round-trip fees = $400, net profit = $322.91

### max_qty impact (Issue 3):
**Before:** Solution has 20-30 legs with qty=10 each  
**After:** Solution has 10-15 legs with qty=20-30 each (more capital-efficient)

### Scenario expansion (Issue 4):
**Before:** Theta table shows 5 rows (crash, dip, base, rally, surge)  
**After:** Theta table shows 9 rows (crash, big_dip, dip, soft_dip, base, soft_rally, rally, big_rally, surge)

### Exit date (Requirement B):
**Before:** P&L calculated at individual option expirations (spread across dates)  
**After:** All P&L calculated at single exit date (e.g., 2026-04-08)

---

## NEXT SESSION WORKFLOW

1. Open this file (IMPLEMENTATION_GUIDE.md)
2. Follow the "IMPLEMENTATION SEQUENCE" section in order
3. After each step, run the testing checklist
4. When done with step 5, begin implementation of Requirements A and B
5. Reference specific line numbers in this guide when making changes
6. If you hit an issue, check the "CRITICAL NEW REQUIREMENTS" section and the plan for context

---

## QUESTIONS FOR NEXT SESSION

1. **Volatility forecasting:** For Requirement A, do you have a specific volatility forecast model, or should we use the scenario IV shifts as-is?
2. **Exit date default:** If user doesn't provide `--exit-date`, should we default to (today + holding_days) or require it always?
3. **P&L floor:** Should the `--pnl-floor` constraint use expiration P&L (worst case) or exit date P&L (actual closing profit)?

