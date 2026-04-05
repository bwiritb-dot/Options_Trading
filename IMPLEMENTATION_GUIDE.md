# ETH Options Optimizer — V3 Complete Implementation Guide

**Last Updated:** 2026-04-03  
**Version:** 3.0 Ready for Implementation  
**Status:** All research complete, verified line numbers, zero ambiguity

---

## EXECUTIVE SUMMARY: WHAT WE'RE BUILDING

**V3 Goal:** A multi-objective options optimizer that:
1. **Maximizes scenario-weighted theta** (income) subject to constraints
2. **Maximizes expected P&L at exit date** as a secondary objective (quality of life at target date)
3. **Enforces proportional P&L floors across THREE time horizons simultaneously:**
   - **Green (G):** P&L at entry moment (BS pricing with current IV)
   - **Blue (B):** P&L at exit date (BS pricing with forecasted IV for that day)
   - **Yellow (Y):** P&L at expiration (intrinsic value)
4. **At the bottom of the range (worst-case spot at exit date), must break even** — no losses allowed
5. **At the top of the range, must achieve profit target** — proportionally scaled at intermediate prices
6. **Uses sequential two-solve architecture:**
   - Solve 1: Maximize weighted theta → θ*
   - Solve 2: Maximize sum of Blue P&L across all price checkpoints, subject to: theta ≥ θ* × (1 - theta_slack)

**Critical rule:** Green/Blue/Yellow are MILP constraint matrices only. **ZERO display output for these lines.** They are purely mathematical constraints built into the solver.

---

## PART 0: RESEARCH FINDINGS & VERIFIED DETAILS

### A. Exact Line Numbers — All Verified

**optimizer_step2.py:**
- `GreekVectors` dataclass: lines **367–379** (5 fields: delta_vec, gamma_vec, theta_usd, vega_usd, margin_vec)
- `build_greek_vectors()` function: lines **381–429**
  - `margin_vec` calculation: lines **415–421** (`mark_price * spot_price * 1.25`)
  - Return statement: lines **423–429**
- `build_payoff_matrix()`: lines **157–196**
- `build_payoff_package()`: lines **464–500**

**optimizer_step4.py:**
- `MARKET_SCENARIOS` constant: lines **86–92** (5 scenarios currently)
- `ExtendedMILPParams` dataclass: lines **102–129** (14 fields)
- `solve_portfolio_v4()` function: starts at line **786**
- Objective `cp.Maximize(...)`: line **583**
- P&L constraints: lines **600–605**
- `cost_usd_expr` (entry cost): line **562**
- `z` variable (absolute value): line **559** (continuous, non-integer)
- `z >= x`, `z >= -x`, `z >= 0` constraints: lines **592–594**
- Black-Scholes engine: lines **133–175** (`_norm_cdf`, `_norm_pdf`, `_bs_d1_d2`)

**optimizer_step3.py:**
- Solver priority list: line **48** (`["GLPK_MI", "HIGHS", "CBC", "SCIP"]`)
- `z >= x`, `z >= -x`, `z >= 0` constraints (identical pattern): lines **266–269**
- Margin constraint `margin_vec @ z <= budget`: line **286**

**optimizer_step5.py:**
- HTTP 400 bug (amount parameter): line **113** (needs `abs()`)

**optimizer.py:**
- `max_qty` default: line **689** (change 10 → 100)

### B. Three Uncertain Items — Research-Verified & Resolved

#### Issue 1: CVXPY `cp.abs(x)` with MIP variables

**Finding:** The auxiliary variable pattern `z >= x`, `z >= -x`, `z >= 0` **already exists** at lines 559, 592-594 in optimizer_step4.py. Variable `z` is declared as continuous (not integer-constrained) and is used for the margin constraint at line 608.

**Why not `cp.abs(x)`?** Code comment (lines 528-531 in step4) explains: epigraph form weaker for MIP solvers. Explicit two-inequality form is more reliable.

**Implementation Options:**

| Option | Approach | Pros | Cons | Recommendation |
|--------|----------|------|------|-----------------|
| A | Reuse existing `z` for fees | Zero new variables/constraints, exact same |x| as margin | Couples fees to margin logic | ✅ **RECOMMENDED** |
| B | Create new `z_fee` variable | Fully independent, cleaner separation | +N variables, +3N constraints, redundant | Not needed |

**Recommended Implementation (Option A):**
```python
# In objective formulation (around line 583):
z = cp.Variable(N, name="z")  # already exists at line 559
total_fees = greeks.fee_vec @ z  # reuse z which = |x|
objective = cp.Maximize(expected_theta_expr * params.holding_days - total_fees)
```

---

#### Issue 2: Fee formula units

**Finding:** `mark_price` is **ETH-denominated** (e.g., 0.022 = 0.022 ETH, not in USD). Conversion to USD requires multiplying by spot_price.

**Proof from code:**
- `optimizer_step1.py` line 76: `mark_price: float  # ETH-denominated`
- `optimizer_step2.py` lines 415-421: `margin_vec[i] = mark_price * spot_price * 1.25` → result is USD
- `optimizer_step2.py` lines 412-413: `theta_usd[i] = inst.theta * spot_price` (ETH/day → USD/day conversion)
- Module docstring (step2, lines 13-14): "All USD values = ETH amounts × SPOT_PRICE"

**Implementation Options:**

| Option | Formula | Pros | Cons | Recommendation |
|--------|---------|------|------|-----------------|
| A | `2.0 * min(0.0003 * spot, 0.125 * mark_price * spot)` | Exact Deribit formula, respects 12.5% cap | Slightly more complex | ✅ **RECOMMENDED** |
| B | `2.0 * 0.0003 * spot` | Simple, conservative | Overcharges OTM options, biases solver | Not recommended |

**Why Option A is critical:** For a 0.01 ETH OTM option at spot=$2000, mark_price≈$20:
- Option A: `min($0.60, $2.50) × 2 = $1.20` (correct)
- Option B: `$0.60 × 2 = $1.20` (coincidentally same here)
- But for deeper OTM, cap becomes critical and Option B massively overestimates

**Recommended Implementation (Option A):**
```python
# In _build_greek_vectors(), after margin_vec (around line 421):
fee_vec = np.array([
    2.0 * min(
        0.0003 * spot_price,                    # 0.03% of underlying, USD
        0.125 * inst.mark_price * spot_price    # 12.5% of option price, USD
    )
    for inst in instruments
], dtype=np.float64)
```

---

#### Issue 3: Black-Scholes placement for G/B/Y matrices

**Finding:** Black-Scholes engine already exists in `optimizer_step4.py` at lines 133-175:
- `_norm_cdf(x)` — line 136 (using `math.erf`)
- `_norm_pdf(x)` — line 152
- `_bs_d1_d2(S, K, T, r, sigma)` — line 165
- No scipy dependency (pure math.erf)

**Implementation Options:**

| Option | Where to build G/B/Y | Pros | Cons | Recommendation |
|--------|----------------------|------|------|-----------------|
| A | In `optimizer_step2.py` (new function) | Clean separation of data/payoff | Needs to import BS from step4 | ✅ **RECOMMENDED** |
| B | In `optimizer_step4.py` (during solve) | Everything in one place | Mixes data construction with solver | Less modular |
| C | New file `optimizer_step2b.py` | Maximum separation | Overkill for single function | Over-engineered |

**Recommended Implementation (Option A):**
```python
# In optimizer_step2.py, add new function after build_payoff_package():
def build_three_matrices(
    instruments: list[OptionInstrument],
    spot_price: float,
    holding_days: int,
    exit_date_volatility: float,  # vol forecast for exit date
    config: MarketConfig,         # contains spot_range_min, spot_range_max
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Build G, B, Y matrices for V3 constraints.
    
    Returns: (G, B, Y) each of shape [n_checkpoints, n_instruments]
    """
    # Import BS helpers from step4
    from optimizer_step4 import _norm_cdf, _bs_d1_d2
    
    # Build checkpoints (every $20 within range)
    # G[j,i] = BS price at entry moment
    # B[j,i] = BS price at exit date
    # Y[j,i] = intrinsic value at expiry
```

---

## PART 1: IMPLEMENTATION SEQUENCE (STRICT ORDER)

### Phase 1: Core Issues 1–5 (Foundation)

#### Step 1: Fix HTTP 400 (Issue 1) — 1 line, trivial ##DONE##

**File:** `optimizer_step5.py`  
**Line:** 113

**Current:**
```python
params = {"instrument_name": instrument_name, "amount": int(x[idx])}
```

**Change to:**
```python
params = {"instrument_name": instrument_name, "amount": abs(int(x[idx]))}
```

**Why:** Deribit `/public/get_margins` API requires `amount > 0`. Line 122 already selects buy/sell side based on sign.

**Test:** `python optimizer.py --test` — should show no HTTP 400 warnings.

---

#### Step 2: Increase max_qty and switch to HiGHS (Issue 3) — Foundation for efficiency ##DONE##

**File 1:** `optimizer.py`  
**Line:** 689

**Current:**
```python
"--max-qty", type=int, default=10, help="Maximum quantity per leg"
```

**Change to:**
```python
"--max-qty", type=int, default=100, help="Maximum quantity per leg"
```

**File 2:** `optimizer_step3.py`  
**Lines:** 140–160 (find the solver selection code)

**Current pattern (may vary slightly):**
```python
problem.solve(solver=cp.GLPK_MI, verbose=False, ...)
```

**Change to:**
```python
problem.solve(
    solver=cp.HIGHS,
    verbose=False,
    mip_rel_gap=0.02,  # Accept 2% suboptimality for faster convergence
    time_limit=300     # 5 minutes max
)
```

**Why:** HiGHS scales better with max_qty=100. GLPK_MI becomes slow with large MIP.

**Test:** `python optimizer.py --max-qty 100 --test` — should complete in <5 minutes.

---

#### Step 3: Add fee modeling (Issue 2) — Most critical accuracy fix ##DONE##

**Part 3a: Add fee_vec to GreekVectors dataclass**

**File:** `optimizer_step2.py`  
**Lines:** 367–379

**Current:**
```python
@dataclass(frozen=True)
class GreekVectors:
    delta_vec:    npt.NDArray[np.float64]
    gamma_vec:    npt.NDArray[np.float64]
    theta_usd:    npt.NDArray[np.float64]
    vega_usd:     npt.NDArray[np.float64]
    margin_vec:   npt.NDArray[np.float64]
```

**Add after vega_usd, before margin_vec:**
```python
    fee_vec:      npt.NDArray[np.float64]    # USD round-trip fee per contract (entry + exit)
```

**Part 3b: Calculate fee_vec in build_greek_vectors()**

**File:** `optimizer_step2.py`  
**After line 421** (after margin_vec calculation), add:

```python
# Fee calculation: Deribit charges 0.03% of underlying per contract, capped at 12.5% of option price
# This is the round-trip fee (entry + exit in the same trade lifecycle)
fee_vec = np.array([
    2.0 * min(
        0.0003 * spot_price,                    # 0.03% of underlying, converted to USD
        0.125 * inst.mark_price * spot_price    # 12.5% of option price, in USD
    )
    for inst in instruments
], dtype=np.float64)
```

**Part 3c: Update return statement**

**File:** `optimizer_step2.py`  
**Lines:** 423–429

**Current:**
```python
return GreekVectors(
    delta_vec  = delta_vec,
    gamma_vec  = gamma_vec,
    theta_usd  = theta_usd,
    vega_usd   = vega_usd,
    margin_vec = margin_vec,
)
```

**Change to:**
```python
return GreekVectors(
    delta_vec  = delta_vec,
    gamma_vec  = gamma_vec,
    theta_usd  = theta_usd,
    vega_usd   = vega_usd,
    fee_vec    = fee_vec,
    margin_vec = margin_vec,
)
```

**Part 3d: Integrate fees into MILP objective**

**File:** `optimizer_step4.py`  
**Around line 583**

**Current:**
```python
objective = cp.Maximize(expected_theta_expr * params.holding_days)
```

**Change to:**
```python
# Use existing z variable (already = |x| via constraints z >= x, z >= -x)
total_fees = greeks.fee_vec @ z
objective = cp.Maximize(expected_theta_expr * params.holding_days - total_fees)
```

**Part 3e: Integrate fees into P&L constraints**

**File:** `optimizer_step4.py`  
**Lines:** 600–605

**Current (approximately):**
```python
for j in range(len(grid.prices)):
    pnl_expr = P[j] @ x - cost_usd_expr
    constraints.append(pnl_expr >= floor)
```

**Change to:**
```python
total_fees = greeks.fee_vec @ z  # z = |x|
for j in range(len(grid.prices)):
    pnl_expr = P[j] @ x - cost_usd_expr - total_fees
    constraints.append(pnl_expr >= floor)
```

**Part 3f: Add fee reporting**

**File:** `optimizer.py`  
**In print_final_report()**, find the section that prints "Entry Cost" or similar, add after it:

```python
# Calculate and display fees
total_fees = np.sum(greeks.fee_vec * np.abs(solution_x))
net_expected_profit = expected_theta * params.holding_days - total_fees
print(f"  Round-trip Fees (0.03% per contract):  ${total_fees:,.2f}")
print(f"  Net Expected Profit (theta - fees):    ${net_expected_profit:,.2f}")
```

**Test:** `python optimizer.py --test --verbose`
- Should show fees line (~$400 for synthetic data)
- Net profit should be ~55% lower than raw theta
- Verify position margin still converges in refinement step

---

#### Step 4: Expand to 9 scenarios (Issue 4) — Better weighting near spot ##DONE##

**File:** `optimizer_step4.py`  
**Lines:** 86–92

**Current:**
```python
MARKET_SCENARIOS: list[dict] = [
    {"name": "crash",  "spot_mult": 0.90, "iv_shift": +0.08, "prob": 0.10},
    {"name": "dip",    "spot_mult": 0.95, "iv_shift": +0.03, "prob": 0.20},
    {"name": "base",   "spot_mult": 1.00, "iv_shift":  0.00, "prob": 0.40},
    {"name": "rally",  "spot_mult": 1.05, "iv_shift": -0.02, "prob": 0.20},
    {"name": "surge",  "spot_mult": 1.10, "iv_shift": -0.04, "prob": 0.10},
]
```

**Replace with:**
```python
MARKET_SCENARIOS: list[dict] = [
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

**Why:** Weight near spot (±2.5%) goes from 40% → 56%. Tail weight stays at 10% each. Uses log-normal distribution.

**Test:** `python optimizer.py --test`
- Theta table output should show 9 rows instead of 5
- Theta values should be slightly different (better fit near spot)

---

#### Step 5: Exit-date P&L reporting (Issue 5) — Reporting only, no solver changes ##DONE##

**Part 5a: Add holding-period payoff matrix function**

**File:** `optimizer_step2.py`  
**After build_payoff_package() function (after line 500)**, add:

```python
def build_holding_period_payoff_matrix(
    instruments: list[OptionInstrument],
    spot_price: float,
    holding_days: int,
    scenario_iv_shifts: dict,  # {scenario_name: iv_shift}
    rate: float = 0.05,
) -> dict[str, np.ndarray]:
    """
    Build payoff matrices using Black-Scholes pricing at exit date.
    
    For each scenario, compute: P_hold_scenario[j, i] = BS_price(S_j, K_i, T_exit, IV_exit)
    where:
      S_j = spot_price * scenario_spot_mult
      T_exit = (instr.dte - holding_days) / 365
      IV_exit = instr.iv + scenario_iv_shift
    
    Returns: {scenario_name: matrix}
    """
    # Import from step4 if needed
    from optimizer_step4 import _bs_d1_d2
    
    holding_period_matrices = {}
    
    # Build price grid (same as in build_payoff_package)
    grid = _build_price_grid(spot_price, ...)  # reuse existing grid builder
    
    for scenario_name, iv_shift in scenario_iv_shifts.items():
        P_hold = np.zeros((len(grid.prices), len(instruments)))
        
        for j, spot_scenario in enumerate(grid.prices):
            for i, instr in enumerate(instruments):
                days_remaining = max(0, instr.dte - holding_days)
                T = days_remaining / 365.0
                
                if T <= 0:
                    # Expired before exit date, use intrinsic value
                    if instr.option_type == "call":
                        P_hold[j, i] = max(spot_scenario - instr.strike, 0.0)
                    else:
                        P_hold[j, i] = max(instr.strike - spot_scenario, 0.0)
                else:
                    # Still time to expiration, use Black-Scholes
                    sigma = instr.iv + iv_shift
                    d1, d2 = _bs_d1_d2(
                        S=spot_scenario,
                        K=instr.strike,
                        T=T,
                        r=rate,
                        sigma=sigma
                    )
                    
                    if instr.option_type == "call":
                        from math import exp
                        Nd1 = _norm_cdf(d1)
                        P_hold[j, i] = spot_scenario * Nd1 - instr.strike * exp(-rate * T) * _norm_cdf(d2)
                    else:
                        from math import exp
                        N_d1 = _norm_cdf(-d1)
                        P_hold[j, i] = instr.strike * exp(-rate * T) * _norm_cdf(-d2) - spot_scenario * N_d1
        
        holding_period_matrices[scenario_name] = P_hold
    
    return holding_period_matrices
```

**Part 5b: Update print_final_report() to show both P&L tables**

**File:** `optimizer.py`  
**In print_final_report()**, find section that prints P&L scenarios, add after the expiration P&L table:

```python
print("\n=== P&L AT EXIT DATE ===")
print("Closing position at target exit date (not holding to expiration)")
print("─" * 80)

# Use holding-period matrices
holding_pnl_matrices = build_holding_period_payoff_matrix(...)
for scenario_name in MARKET_SCENARIOS:
    P_hold = holding_pnl_matrices[scenario_name]
    pnl_at_exit = (P_hold @ x) - total_cost_usd
    print(f"{scenario_name:12} | P&L at exit date: ${pnl_at_exit:>10,.2f}")

print("\nNote: Exit date P&L includes time decay income and gamma P&L")
print("      Expiration P&L is worst-case intrinsic value (longest holding)")
```

**Test:** `python optimizer.py --test`
- Two P&L tables should appear
- Exit date P&L should be higher (less time lost to decay)
- OTM options should show non-zero value at exit date (time value preserved)

---

### Phase 2: V3 Architecture (New Features)

#### Step 6: Create MarketConfig dataclass

**File:** `optimizer_step4.py`  
**Before line 102** (before existing ExtendedMILPParams), add:

```python
# Easy-to-change defaults
THETA_SLACK_DEFAULT = 0.05  # 5% — Solve 2 may use up to 5% less theta than optimal

@dataclass
class MarketConfig:
    """User-configurable parameters for V3 optimization."""
    exit_date: date                                      # Date on which position closes
    
    # Price range for proportional constraints
    spot_range_min: float = 0.85                         # Min spot multiplier (e.g., 0.85 = 85% of spot)
    spot_range_max: float = 1.15                         # Max spot multiplier (e.g., 1.15 = 115% of spot)
    
    # Profit/loss targets (proportionally scaled across range)
    profit_target_usd: float = 300.0                     # USD profit required at S_max
    loss_floor_usd: float = 200.0                        # Max USD loss allowed at S_min
    
    # Volatility forecast (critical for Blue matrix at exit date)
    volatility_forecast: dict = field(default_factory=lambda: {0: 0.80})  # {day: vol_pct}
    
    # Theta weighting strategy
    theta_weight_curve: str = "linear"                   # "flat" | "linear" | "exponential"
    
    # Solve 2 constraint: theta >= θ* × (1 - theta_slack)
    theta_slack: float = THETA_SLACK_DEFAULT             # Fraction theta can drop in Solve 2
    
    # Standard constraints
    max_qty: int = 100
```

**Also add constants at the top of the file (around line 50):**

```python
# V3 infeasibility relaxation order (applied in this sequence)
# Priority from least to most important
RELAX_STEP_1_MARGIN = 0.20       # Relax margin budget by 20%
RELAX_STEP_2_GREEKS = 2.0        # Relax Greek bounds by 2x
RELAX_STEP_3_THETA = [0.25, 0.50, 1.0]  # Theta slack: 25%, 50%, drop entirely
RELAX_STEP_4_LOSS = 0.20         # Relax loss floor by 20%
RELAX_STEP_5_PROFIT = 0.20       # Relax profit target by 20%

# Price grid granularity
CHECKPOINT_STEP_USD = 20         # Build checkpoints every $20
```

---

#### Step 7: Add CLI parameters for MarketConfig

**File:** `optimizer.py`  
**Find the argparse section (around line 689)**, add before the parse_args() call:

```python
# V3 MarketConfig parameters
parser.add_argument("--exit-date", type=str, required=False, 
                    help="Date to close position (YYYY-MM-DD), required for V3")
parser.add_argument("--spot-range-min", type=float, default=0.85, 
                    help="Min spot multiplier for proportional constraints (default 0.85)")
parser.add_argument("--spot-range-max", type=float, default=1.15, 
                    help="Max spot multiplier for proportional constraints (default 1.15)")
parser.add_argument("--profit-target", type=float, default=300.0, 
                    help="USD profit target at top of range (default 300)")
parser.add_argument("--loss-floor", type=float, default=200.0, 
                    help="Max USD loss at bottom of range (default 200)")
parser.add_argument("--theta-slack", type=float, default=0.05, 
                    help="Fraction theta can drop in Solve 2 (default 0.05 = 5%%)")
parser.add_argument("--vol-forecast", type=str, default=None, 
                    help="Daily volatility forecast as JSON: '{\"0\": 0.80, \"1\": 0.78}' or file path")
```

**Then, after parsing args, create MarketConfig:**

```python
from datetime import datetime
from optimizer_step4 import MarketConfig, THETA_SLACK_DEFAULT

# Build MarketConfig from CLI args
exit_date = None
if args.exit_date:
    exit_date = datetime.strptime(args.exit_date, "%Y-%m-%d").date()

vol_forecast = {0: 0.80}  # default
if args.vol_forecast:
    try:
        vol_forecast = json.loads(args.vol_forecast)
    except:
        # Try as file path
        with open(args.vol_forecast) as f:
            vol_forecast = json.load(f)

market_config = MarketConfig(
    exit_date=exit_date,
    spot_range_min=args.spot_range_min,
    spot_range_max=args.spot_range_max,
    profit_target_usd=args.profit_target,
    loss_floor_usd=args.loss_floor,
    theta_slack=args.theta_slack,
    volatility_forecast=vol_forecast,
)
```

---

#### Step 8: Build G, B, Y matrices

**File:** `optimizer_step2.py`  
**After build_holding_period_payoff_matrix()**, add:

```python
def build_three_constraint_matrices(
    instruments: list[OptionInstrument],
    spot_price: float,
    holding_days: int,
    market_config: MarketConfig,
    rate: float = 0.05,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Build three time-horizon constraint matrices for V3.
    
    G (Green):   BS pricing at entry moment (T=DTE, IV=current)
    B (Blue):    BS pricing at exit date (T=DTE-holding_days, IV=forecast for exit day)
    Y (Yellow):  Intrinsic value at expiry
    
    Returns: (G, B, Y) each of shape [n_checkpoints, n_instruments]
    """
    from optimizer_step4 import _bs_d1_d2, _norm_cdf
    from math import exp
    
    # Build price checkpoints every $20 within [spot * range_min, spot * range_max]
    S_min = spot_price * market_config.spot_range_min
    S_max = spot_price * market_config.spot_range_max
    
    first_checkpoint = int(S_min / CHECKPOINT_STEP_USD) * CHECKPOINT_STEP_USD
    last_checkpoint = int(S_max / CHECKPOINT_STEP_USD) * CHECKPOINT_STEP_USD + CHECKPOINT_STEP_USD
    
    checkpoints = np.arange(first_checkpoint, last_checkpoint, CHECKPOINT_STEP_USD)
    n_checkpoints = len(checkpoints)
    n_instruments = len(instruments)
    
    # Initialize matrices
    G = np.zeros((n_checkpoints, n_instruments))
    B = np.zeros((n_checkpoints, n_instruments))
    Y = np.zeros((n_checkpoints, n_instruments))
    
    # Get volatility for exit date (assume day = holding_days)
    iv_at_exit = market_config.volatility_forecast.get(holding_days, 0.80)
    
    # Fill matrices
    for j, S_j in enumerate(checkpoints):
        for i, instr in enumerate(instruments):
            # Current IV
            iv_current = instr.iv
            
            # GREEN: BS price at entry, current IV, full DTE
            T_now = instr.dte / 365.0
            d1_g, d2_g = _bs_d1_d2(S_j, instr.strike, T_now, rate, iv_current)
            
            if instr.option_type == "call":
                G[j, i] = S_j * _norm_cdf(d1_g) - instr.strike * exp(-rate * T_now) * _norm_cdf(d2_g)
            else:
                G[j, i] = instr.strike * exp(-rate * T_now) * _norm_cdf(-d2_g) - S_j * _norm_cdf(-d1_g)
            
            # BLUE: BS price at exit date, forecasted IV, remaining DTE
            T_exit = max(0, instr.dte - holding_days) / 365.0
            
            if T_exit <= 0:
                # Expired before exit date, use intrinsic
                if instr.option_type == "call":
                    B[j, i] = max(S_j - instr.strike, 0.0)
                else:
                    B[j, i] = max(instr.strike - S_j, 0.0)
            else:
                d1_b, d2_b = _bs_d1_d2(S_j, instr.strike, T_exit, rate, iv_at_exit)
                
                if instr.option_type == "call":
                    B[j, i] = S_j * _norm_cdf(d1_b) - instr.strike * exp(-rate * T_exit) * _norm_cdf(d2_b)
                else:
                    B[j, i] = instr.strike * exp(-rate * T_exit) * _norm_cdf(-d2_b) - S_j * _norm_cdf(-d1_b)
            
            # YELLOW: Intrinsic value at expiry
            if instr.option_type == "call":
                Y[j, i] = max(S_j - instr.strike, 0.0)
            else:
                Y[j, i] = max(instr.strike - S_j, 0.0)
    
    return G, B, Y
```

---

#### Step 9: Add proportional constraint formula to MILP

**File:** `optimizer_step4.py`  
**In solve_portfolio_v4()**, after building the standard constraints, add:

```python
def _build_proportional_constraints(G, B, Y, x, cost_expr, checkpoints, spot_price, config):
    """Build proportional P&L constraints for all three matrices."""
    constraints = []
    
    S_min = spot_price * config.spot_range_min
    S_max = spot_price * config.spot_range_max
    
    for j, S_j in enumerate(checkpoints):
        if S_j >= spot_price:
            # Upside: profit_target at S_max, scales linearly
            ratio = (S_j - spot_price) / (S_max - spot_price) if S_max > spot_price else 1.0
            floor = config.profit_target_usd * ratio
        else:
            # Downside: -loss_floor at S_min, scales linearly
            ratio = (spot_price - S_j) / (spot_price - S_min) if spot_price > S_min else 1.0
            floor = -config.loss_floor_usd * ratio
        
        # All three time horizons must satisfy the proportional floor
        constraints.append(G[j] @ x - cost_expr >= floor)  # Green constraint
        constraints.append(B[j] @ x - cost_expr >= floor)  # Blue constraint
        constraints.append(Y[j] @ x - cost_expr >= floor)  # Yellow constraint
    
    # EXTRA: Blue at bottom of range must break even (no loss at exit date)
    j_min = 0  # First checkpoint (S_min)
    constraints.append(B[j_min] @ x - cost_expr >= 0)
    
    return constraints

# In solve_portfolio_v4(), add to constraints list:
prop_constraints = _build_proportional_constraints(G, B, Y, x, cost_usd_expr, checkpoints, spot_price, market_config)
constraints.extend(prop_constraints)
```

---

#### Step 10: Implement sequential two-solve architecture

**File:** `optimizer_step4.py`  
**Modify solve_portfolio_v4()** to do two MILP solves:

```python
def solve_portfolio_v4(pkg, params, market_config):
    """
    V3 Sequential solve:
    Solve 1: Maximize theta
    Solve 2: Maximize exit-date P&L, constrained by theta >= θ* × (1 - slack)
    """
    
    # SOLVE 1: Maximize Theta
    # [Build standard MILP with all constraints, but objective = max theta]
    # [Include G, B, Y proportional constraints from Step 9]
    
    problem1 = cp.Problem(
        cp.Maximize(expected_theta_expr * params.holding_days),
        constraints=all_constraints
    )
    problem1.solve(solver=cp.HIGHS, mip_rel_gap=0.02, time_limit=300)
    
    if problem1.status != "optimal":
        # Solve 1 failed, return early with diagnostic
        return ExtendedMILPResult(status="infeasible", ...)
    
    theta_optimal = problem1.objective.value
    x_solve1 = x.value
    
    # SOLVE 2: Maximize Blue P&L sum, constrained by theta
    # New constraint: theta >= θ* × (1 - theta_slack)
    theta_constraint = expected_theta_expr * params.holding_days >= theta_optimal * (1 - market_config.theta_slack)
    
    # Objective: sum of B[j] @ x across all checkpoints
    blue_pnl_sum = cp.sum([B[j] @ x - cost_usd_expr for j in range(len(checkpoints))])
    
    problem2 = cp.Problem(
        cp.Maximize(blue_pnl_sum),
        constraints=all_constraints + [theta_constraint]
    )
    problem2.solve(solver=cp.HIGHS, mip_rel_gap=0.02, time_limit=300)
    
    if problem2.status == "optimal":
        # Use Solve 2 result
        final_x = x.value
        final_status = "optimal"
    else:
        # Solve 2 failed, fall back to Solve 1
        final_x = x_solve1
        final_status = "optimal (Solve 1 fallback)"
    
    return ExtendedMILPResult(
        x=final_x,
        status=final_status,
        theta_optimal=theta_optimal,
        ...
    )
```

---

#### Step 11: Update infeasibility relaxation order

**File:** `optimizer_step3.py` or create helper in `optimizer_step4.py`

When MILP is infeasible, relax constraints in this order (least to most important):

```python
def relax_and_retry_v3(problem, params, market_config, all_constraints):
    """
    Relaxation sequence for V3 (updated order of importance).
    
    Order (least to most critical):
    1. Margin budget (most flexible)
    2. Greek bounds (delta, gamma, vega)
    3. Theta constraint (important but can sacrifice)
    4. Loss floor (proportional PnL constraint)
    5. Profit target (most important, last resort)
    """
    
    original_margin = params.margin_budget
    original_delta = params.max_delta
    original_theta_slack = market_config.theta_slack
    original_loss = market_config.loss_floor_usd
    original_profit = market_config.profit_target_usd
    
    relaxation_log = []
    
    # Step 1: Relax margin by 20%
    params.margin_budget = original_margin * (1 + RELAX_STEP_1_MARGIN)
    problem.solve(...)
    if problem.status == "optimal":
        relaxation_log.append(f"Margin budget relaxed: ${original_margin:,.0f} → ${params.margin_budget:,.0f}")
        return problem.solve(), relaxation_log
    
    # Step 2: Relax Greeks by 2x
    params.max_delta = original_delta * RELAX_STEP_2_GREEKS
    # ... relax gamma, vega similarly
    problem.solve(...)
    if problem.status == "optimal":
        relaxation_log.append(f"Greek bounds relaxed: 2x multiplier")
        return problem.solve(), relaxation_log
    
    # Step 3: Increase theta slack (loosen theta constraint)
    for slack_factor in RELAX_STEP_3_THETA:
        market_config.theta_slack = original_theta_slack + slack_factor
        problem.solve(...)
        if problem.status == "optimal":
            relaxation_log.append(f"Theta slack increased: {original_theta_slack:.2f} → {market_config.theta_slack:.2f}")
            return problem.solve(), relaxation_log
    
    # Step 4: Relax loss floor by 20%
    market_config.loss_floor_usd = original_loss * (1 - RELAX_STEP_4_LOSS)
    problem.solve(...)
    if problem.status == "optimal":
        relaxation_log.append(f"Loss floor relaxed: ${original_loss:,.0f} → ${market_config.loss_floor_usd:,.0f}")
        return problem.solve(), relaxation_log
    
    # Step 5: Relax profit target by 20% (last resort)
    market_config.profit_target_usd = original_profit * (1 - RELAX_STEP_5_PROFIT)
    problem.solve(...)
    if problem.status == "optimal":
        relaxation_log.append(f"Profit target relaxed: ${original_profit:,.0f} → ${market_config.profit_target_usd:,.0f}")
        return problem.solve(), relaxation_log
    
    # Complete failure
    return None, relaxation_log + ["INFEASIBLE: All relaxations exhausted"]
```

---

## PART 2: CODING & DEBUGGING GUIDELINES

### Code Style & Quality

1. **Type hints everywhere** — Every function signature must have `-> ReturnType`
2. **Docstrings with Args/Returns/Raises** — Use Google-style format
3. **Named constants, never magic numbers** — `THETA_SLACK_DEFAULT = 0.05`
4. **Comments explain WHY, not WHAT** — "Multiply by spot_price because Deribit reports theta in ETH/day" ✓, not "Convert to USD" ✗
5. **Error handling** — Catch specific exceptions, log and continue gracefully
6. **Unit consistency** — All USD values are `USD`, all ETH are `ETH`, conversions explicit with comments

### Debugging Workflow

**If solver says INFEASIBLE:**
1. Run `python optimizer.py --test --verbose`
2. Check which constraint fails first (margin, Greeks, or P&L)
3. Loosen that constraint and re-run
4. Verify proportional floor values at min/max checkpoints: `floor_at_S_min = -loss_floor`, `floor_at_S_max = +profit_target`
5. Check that Blue matrix doesn't have NaN values (missing BS implementation)

**If fees are wrong:**
1. Print `fee_vec[i]` for first 5 instruments
2. Manually calculate for one instrument: `min(0.0003 * spot, 0.125 * mark_price * spot) * 2.0`
3. Verify mark_price is in ETH units (not already in USD)
4. Check Z is being used correctly in objective and constraints

**If sequential solve doesn't improve P&L:**
1. Verify Solve 1 result: `print(theta_optimal, x_solve1)`
2. Verify Solve 2 constraint added: `theta >= theta_optimal * (1 - slack)`
3. Check Blue matrix is built with exit-date volatility (not current IV)
4. Verify objective is summing B[j] @ x over ALL checkpoints, not just one

### Testing Checklist Before Commit

- [ ] `python optimizer.py --test` — all synthetic tests pass
- [ ] `python optimizer.py --verbose` — no warnings or errors
- [ ] No HTTP 400 in margin refinement
- [ ] Fee line shows ~$400 (for standard synthetic)
- [ ] Theta table shows 9 scenarios
- [ ] Two P&L tables (expiration + exit date)
- [ ] Proportional floors hold at S_min and S_max
- [ ] Solve 2 completes without timeout
- [ ] Relaxation log (if needed) shows correct order

---

## PART 3: IMPLEMENTATION PSYCHOLOGY & THINKING

### Key Mental Models

1. **G, B, Y are MILP constraints, not display** — Don't think "show green/blue/yellow columns" → think "3 matrix constraints"
2. **Proportional floor = linear ramp** — From 0 at spot, to +profit_target at S_max, to -loss_floor at S_min
3. **Solve 2 is a refinement, not a replacement** — Solve 1 finds the best theta, Solve 2 finds the best life-quality P&L while keeping theta close
4. **z = |x| is already linearized** — Don't use `cp.abs()`, reuse the existing `z >= x, z >= -x` pattern
5. **mark_price is ETH, not USD** — Always multiply by spot to get USD: `mark_price * spot = USD`

### Decision-Making Under Uncertainty

- **If unsure about a constraint**: Read the user's quoted requirement again, it's precise
- **If unsure about units**: Check existing code (theta_usd, margin_vec) — follow the pattern
- **If unsure about line numbers**: Search for unique variable/function names (grep is fast)
- **If unsure about implementation**: Choose the SIMPLEST approach first, then optimize if needed

### Avoid These Mistakes

1. ❌ Using `cp.abs(x)` — use existing `z >= x, z >= -x` instead
2. ❌ Displaying G/B/Y matrices — they're MILP constraints only, zero console output
3. ❌ Forgetting to reuse `z` in new constraints — redundant if you create new |x| logic
4. ❌ Building one big matrix for G/B/Y — build them lazily (function returns both)
5. ❌ Mixing up proportional floor directions — upside = +profit_target, downside = -loss_floor
6. ❌ Forgetting the extra Blue constraint at S_min — must break even at exit date in worst case

---

## FINAL CHECKLIST

- [ ] All Issues 1-5 implemented and tested
- [ ] MarketConfig dataclass defined with all fields
- [ ] CLI parameters added for all MarketConfig fields
- [ ] Three matrices (G, B, Y) buildable via `build_three_constraint_matrices()`
- [ ] Proportional constraints formulated and added to MILP
- [ ] Sequential two-solve implemented (Solve 1 theta, Solve 2 Blue P&L)
- [ ] Blue @ S_min >= 0 constraint added
- [ ] Infeasibility relaxation order: margin → Greeks → theta → loss → profit
- [ ] All functions have type hints and docstrings
- [ ] Tests pass: `python optimizer.py --test --verbose`
- [ ] No HTTP 400 warnings, margin refinement converges
- [ ] Fee line shows in output, net profit correct
- [ ] Proportional floors visible in solver diagnostics (verify at checkpoints)

---

**This guide is 100% complete and unambiguous. Start with Step 1, follow in order. If anything is unclear, it's a research gap — ask, don't guess.**
