# V2 Design Research — Token-Efficient Analysis

**Date**: 2026-04-03  
**Purpose**: Design v2 modifications (real data pricing updates)  
**Method**: Code inspection + technical analysis (no web searches)

---

## 1. MARGIN REFINEMENT RE-SOLVE: OPTIONS ANALYSIS

### Current Implementation (Step 5)
- Line 256-257: "Otherwise, update margin_vec, rebuild PayoffPackage, re-solve"
- Line 324: `current_result = refine_margins(...)` followed by new `solve_portfolio_v4()` call
- **Verdict: YES, it DOES re-solve the MILP** (not just update vector)

### Option A: FULL RE-SOLVE (Current Implementation) ✅
**Approach**: Query real margins → if over threshold, rebuild PayoffPackage, re-solve MILP
```
Iteration 1: solve_portfolio_v4() with approx margin
↓
Fetch real margins from Deribit API
↓
If real_margin > budget×0.95:
  └─ Update margin_vec → rebuild PayoffPackage → re-solve MILP
  
Max 3 iterations
```

**Pros:**
- ✅ Position adapts to **actual exchange margin requirements**
- ✅ Can reduce qty if real margin > budget (finds feasible alternative)
- ✅ Reflects real trading environment (margin rules vary by leg)
- ✅ Catches approximation errors in margin calculation

**Cons:**
- ❌ 3 additional API calls per iteration (slow)
- ❌ Re-solve may change position entirely (unpredictable)
- ❌ Converges only if feasible at lower position

**Risk**: If real margin is always over budget → infeasible → empty position

---

### Option B: VECTOR-ONLY UPDATE (Alternative)
**Approach**: Query real margins → update margin_vec → BUT don't re-solve, just validate

**Pros:**
- ✅ Fast (only 1 API query)
- ✅ Position stable (no re-solve shock)

**Cons:**
- ❌ Position may actually violate real margin! (constraint wrong)
- ❌ Not trading on real exchange constraints
- ❌ Defeats purpose of refinement

**Verdict**: ❌ **REJECT** — financially incorrect

---

### **RECOMMENDATION: KEEP OPTION A (Current)**
- Current code is correct
- Risk: optimize timeout + retry logic if API slow
- Future: add `--no-refine` flag for speed vs accuracy tradeoff

---

## 2. IV STRESS CALCULATION: OPTIONS ANALYSIS

### Current Implementation (Step 4)
```python
# Line 442: sigma = inst.mark_iv / 100.0
# Line 452: sigma_stressed = max(MIN_IV, sigma + iv_stress_shift)  # +25 ppts
# Line 415-424: IV stress = (price at IV+25pp) - (price at current IV)
#               Constraint: iv_stress_pnl ≥ PNL_FLOOR × 1.5
```

---

### Option A: ABSOLUTE IV SHIFT (Current Implementation) ✅
**Formula**: `sigma_stressed = sigma + 0.25`  
Example: If mark_iv = 45%, then stressed_iv = 70%

**Pros:**
- ✅ Matches spec exactly (IV_STRESS_SHIFT = 0.25 = 25 basis points)
- ✅ Tail risk: big IV move is **additive**, not multiplicative
- ✅ Conservative (worst case: IV explodes in crash)
- ✅ Simple, deterministic, testable

**Cons:**
- ⚠️ May overshoot if IV already high (e.g., 80% + 25% = 105% → capped at MAX_IV)
- ⚠️ Unrealistic for low-IV scenarios (5% + 25% = 30% is huge 6x move)

**Math Check** (IV stress at each price point):
- IV stress is **independent of spot moves** (only IV changes)
- Cost = ∫ vega dσ ≈ vega × Δσ = vega × 0.25
- NOT part of scenario theta (scenarios move spot + IV together)

---

### Option B: PERCENTAGE IV SHIFT (Alternative)
**Formula**: `sigma_stressed = sigma × 1.25`  
Example: If mark_iv = 45%, then stressed_iv = 56.25%

**Pros:**
- ✅ Scales with market regime (high IV → bigger move in basis points)

**Cons:**
- ❌ Not in spec (spec says +25 ppts absolute)
- ❌ Overstates risk in low-IV environments
- ❌ Inconsistent with tail risk concept

**Verdict**: ❌ **REJECT** — contradicts spec

---

### Option C: PIECEWISE (Hybrid)
**Formula**: 
```python
if sigma < 0.30:
    sigma_stressed = 0.55  # Floor to 55% in calm markets
else:
    sigma_stressed = sigma + 0.25  # Add 25pp in stressed markets
```

**Pros:**
- ✅ Avoids extreme moves in calm markets

**Cons:**
- ❌ Not in spec
- ❌ Adds complexity

**Verdict**: ❌ **REJECT** — over-engineering

---

### **RECOMMENDATION: KEEP OPTION A**
- Current: ✅ **Correct and spec-compliant**
- Each price point S_j uses: **mark_iv + 25%** for stress calc
- Independent of scenario spot moves (orthogonal risks)

---

## 3. BID/ASK IN SCENARIOS: CLARIFICATION

### Question
When computing theta in 5 scenarios (crash/dip/base/rally/surge), should bid/ask prices change or stay fixed?

### Current Code Behavior (Step 4, Line 371)
```python
sigma = max(MIN_IV, inst.mark_iv / 100.0 + scenario["iv_shift"])
# Recalculates mark price using BS with NEW IV
# BUT bid/ask prices remain from CURRENT market
```

---

### Option A: FIXED BID/ASK (Current)
**Logic**: 
- Spot changes by scenario multiplier (0.9, 0.95, 1.0, 1.05, 1.10)
- IV changes by scenario shift (-0.04 to +0.08)
- **BID/ASK stay constant** (frozen at current market snapshot)

**Pros:**
- ✅ Entry cost doesn't change (position remains same cost)
- ✅ Pure theta evaluation (excludes bid/ask drift)
- ✅ Computationally simple
- ✅ Consistent with "holding position" logic

**Cons:**
- ⚠️ Unrealistic: bid/ask also move with spot/IV
- ⚠️ May overestimate entry cost vs exit cost in scenarios

**Assumption**: Trader enters NOW (at current bid/ask), holds for 5 days → theta accrues

---

### Option B: SCALED BID/ASK (Alternative)
**Logic**: Assume bid/ask spread scales with mark price change
```python
spread_pct = (ask - bid) / mark
# In scenario:
scenario_ask = scenario_price × (1 + spread_pct/2)
scenario_bid = scenario_price × (1 - spread_pct/2)
```

**Pros:**
- ✅ More realistic market behavior

**Cons:**
- ❌ Complex, no basis in spec
- ❌ Adds slippage model without data
- ❌ Changes "holding days" interpretation

---

### Option C: LINEAR INTERPOLATION
**Logic**: bid/ask move proportionally to price/IV change

**Pros**: Better than fixed

**Cons**: Over-complex for purpose

---

### **RECOMMENDATION: KEEP OPTION A**
- **Interpretation**: Theta is profit from time decay, **not** from bid/ask tightening
- Position entered at **current bid/ask**, held for **5 days**
- Scenarios evaluate how delta/theta change with market moves
- This matches trading reality: once you enter, entry cost is sunk

---

## 4. OUTPUT FORMAT: SPECIFICATION CHANGE

### Your Requirement
```
buy CALL 1200 (strike) 3 apr (expiration)
buy PUT 1200 (strike) 3 apr (expiration)
sell PUT 1400 (strike) 3 apr (expiration)
```

### Current Format (Step 4, Lines 185-215)
```
OPTIMAL POSITION [Solver: GLPK_MI | Status: OPTIMAL]
──────────────────────────────────────────────────────
  Leg  Type  Strike  Expiry      Dir    Qty  Premium   Entry $
   1   CALL  2,100   16 Jan 25   SHORT   -3   $38.50   $115.50
   2   CALL  2,200   16 Jan 25   LONG    +3   $11.20   -$33.60
```

### V2 Format Required
```
BUY CALL 1200 3 April 2025
BUY PUT 1200 3 April 2025
SELL PUT 1400 3 April 2025
BUY CALL 2100 16 January 2025
...
```

### Changes Needed
1. **Output type**: List format, not table
2. **Action words**: "BUY" (qty > 0), "SELL" (qty < 0)
3. **Date format**: "3 April 2025" (not "3 Apr" or "16 Jan 25")
4. **Remove**: Premium, Entry $, other metrics
5. **Add to separate section**: Net Greeks, P&L scenarios, Risk metrics
6. **Keep**: All validation output (constraints, P&L at prices)

### Implementation
- New function: `print_position_list()` → simple loop over active_legs
- Keep `print_final_report()` for detailed analytics
- Or: add flag `--format list` vs `--format detailed`

---

## 5. EDGE CASES: MARGIN/RISK RELAXATION

### Current Spec
- PNL_FLOOR: -400 USD (minimum loss allowed)
- MARGIN_BUDGET: 5000 USD
- Relaxation: Step 1 → Step 2 → Step 3 → INFEASIBLE

### Your Clarification
"Edge cases: change margin or risk requirements (profit/loss must stay the same. Delta and Greeks might change, but it must be specified in output.)"

### What This Means
**When INFEASIBLE at original constraints:**
1. Relax PNL_FLOOR by 20% → -480 USD (worse loss allowed)
2. If still infeasible: relax MARGIN_BUDGET by 20% → 6000 USD
3. If still infeasible: relax MAX_DELTA by 2x → 0.30 (delta constraint looser)
4. If all fail: **return empty position with diagnostics**

### Output Required
```
OPTIMAL POSITION [Solver: GLPK_MI | Status: OPTIMAL (relaxed 2/3)]
────────────────────────────────────────
  [Position details]

RELAXATION APPLIED:
  • Step 1: PNL_FLOOR relaxed -400 → -480 USD
  • Step 2: MARGIN_BUDGET relaxed 5000 → 6000 USD
  [Greeks may change due to larger position allowed]
```

### Definition: RELAXATION
**Relaxation** = automatically loosening constraints when MILP is infeasible
- Goal: Find best feasible solution with **minimal risk loosening**
- Order matters: loosen least restrictive constraint first
- Track which step enabled feasibility (for transparency)

---

## 6. LIBRARY vs CLI CALLABLE: OPTIONS ANALYSIS

### Current Code
- `optimizer.py` = CLI entry point
- Modules are importable: `from optimizer_step1 import fetch_liquid_options`
- Each step has `if __name__ == "__main__": main()` for standalone testing

---

### Option A: CLI-PRIMARY (Current)
**Design**:
```python
# Public API:
optimizer.py --test
optimizer.py --spot 2500
optimizer.py --verbose

# Library use (secondary):
from optimizer_step4 import solve_portfolio_v4
result = solve_portfolio_v4(pkg, params)
```

**Pros:**
- ✅ Simple entry point (`python optimizer.py`)
- ✅ Users don't need Python knowledge
- ✅ Each step independently importable for experts

**Cons:**
- ⚠️ Library API not documented (not primary use case)

**Current Status**: ✅ **WORKING WELL**

---

### Option B: PACKAGE (Alternative)
**Design**: Create `eth_optimizer/` package with `__init__.py`
```python
from eth_optimizer import OptimizeConfig, run_optimizer

cfg = OptimizeConfig(spot=2000, margin=5000)
result = run_optimizer(cfg)
```

**Pros:**
- ✅ Clean library interface
- ✅ Easier integration for other apps

**Cons:**
- ❌ Extra complexity (packaging, imports)
- ❌ Your use case = CLI (one-time optimization)
- ❌ Requires refactoring

**Verdict**: ❌ **OVERKILL for v2** (defer to v3)

---

### Option C: BOTH (Dual Interface)
**Design**: Keep CLI primary, add clean library wrapper
```python
# optimizer_api.py (NEW)
async def optimize(
    spot_price: float = 2000,
    margin_budget: float = 5000,
    use_live: bool = True,
) -> dict:
    """Public API for library users."""
    pkg = await fetch_liquid_options()
    params = ExtendedMILPParams(margin_budget=margin_budget, ...)
    result = solve_portfolio_v4(pkg, params)
    return {"position": result, "margin_used": ...}

# optimizer.py (CLI wrapper)
async def main():
    args = build_argparse()
    result = await optimize(
        spot_price=args.spot,
        margin_budget=args.margin,
        use_live=not args.test,
    )
    print_final_report(result)
```

**Pros:**
- ✅ CLI and library both supported
- ✅ Single implementation (no duplication)
- ✅ Clean separation

**Cons:**
- ⚠️ Minor refactoring needed

---

### **RECOMMENDATION: OPTION C (defer to v2.1)**
- **v2.0**: Keep current CLI structure (works fine)
- **v2.1**: Add `optimizer_api.py` wrapper if needed by users

---

## 7. INFEASIBILITY HANDLING: CLARIFICATION

### Definition
**Infeasible** = No feasible solution exists that satisfies ALL constraints simultaneously

**Example**:
```
Variables: x (position)
Constraints:
  • P&L ≥ -400 (floor)
  • Margin ≤ 5000 (budget)
  • |delta| ≤ 0.15 (delta limit)
```
If these 3 conflict → INFEASIBLE

### Current Handling (Step 3, Lines 600-670)
```python
def relax_and_retry(pkg, params, ...)
    for step in [1, 2, 3]:
        relaxed_params = _relax_params(params, step)
        result = solve_portfolio(pkg, relaxed_params)
        if result.status == OPTIMAL:
            return result  # ← Found feasible!
    
    # All 4 steps failed (original + 3 relaxations)
    return MILPResult(status=INFEASIBLE, x=None, ...)
```

### Step 1: PNL_FLOOR × 1.2
- Original: floor = -400 USD
- Relaxed: floor = -400 × 1.2 = -480 USD (allow 20% worse loss)

### Step 2: MARGIN_BUDGET × 1.2
- Original: budget = 5000 USD
- Relaxed: budget = 5000 × 1.2 = 6000 USD (allow 20% more margin)

### Step 3: MAX_DELTA × 2.0
- Original: delta_max = 0.15
- Relaxed: delta_max = 0.15 × 2 = 0.30 (twice as much directional risk)

### If ALL FAIL
Currently: Return INFEASIBLE with diagnostics → CLI prints error message

**Your Requirement**: Track which constraints relax + display in output ✅

---

## 8. LIVE MODE REQUIREMENT

**Your Decision**: "Real data is CRITICAL for this project"

### Current Status
- **Live mode**: `python optimizer.py` (fetches from Deribit)
- **Test mode**: `python optimizer.py --test` (synthetic data)

### v2 Required
- Keep both modes operational
- Default = **LIVE** (fetch real data)
- Test mode = fallback / CI/CD only

**Action**: No change needed — already correct

---

## SUMMARY: V2 MODIFICATIONS (1.0 → 2.0)

| # | Change | Status | Effort |
|---|--------|--------|--------|
| **1** | Output format: list + full details | Research complete | Medium |
| **2** | Margin refinement re-solve | ✅ Already correct | None |
| **3** | IV stress calculation | ✅ Already correct | None |
| **4** | Bid/ask in scenarios | ✅ Fixed (correct) | None |
| **5** | Relaxation tracking in output | Research complete | Small |
| **6** | Edge case handling (risks/margin) | Research complete | Small |
| **7** | Live data mode (ETHUSDT.P update) | **NEW** | Large |
| **8** | Library vs CLI | Defer to v2.1 | — |

---

## MODIFICATION #7: REAL-TIME PRICE UPDATES (CRITICAL)

### Current Implementation
- Spot price: Fetched once via `/public/get_index` or hardcoded (2000)
- Used for: Scenario payoffs, P&L calculations, margin approximation
- **Problem**: Optimization assumes spot is static during solver run (~1-30 seconds)

### v2 Requirement: Dynamic ETHUSDT.P Updates

**Option A: POLLING (Every 100ms)**
```python
async def optimize_with_live_spot():
    spot_initial = 2000
    
    async def get_live_spot():
        while solving:
            spot_current = await fetch_spot_price()  # async call
            update_scenario_payoffs(spot_current)  # Rebuild matrix?
    
    task_spot = asyncio.create_task(get_live_spot())
    result = solve_portfolio_v4(pkg, params)
    task_spot.cancel()
```

**Pros**: Always current  
**Cons**: Solver restarts constantly (inefficient)

**Option B: PERIODIC REFRESH (At solve start + midway check)**
```python
spot_start = await fetch_spot_price()
result = solve_portfolio_v4(pkg, params)

if time_elapsed > 10s:
    spot_updated = await fetch_spot_price()
    if abs(spot_updated - spot_start) > 50:  # ±2.5%
        # Re-solve with new spot? Or warn user?
```

**Pros**: Balanced  
**Cons**: Still disrupts solve

**Option C: ACCEPT STALENESS (Current)**
```python
spot = await fetch_spot_price()  # Once
result = solve_portfolio_v4(pkg, params)  # Uses same spot for 30s
# Report: "Optimization based on $2000 spot (queried 2026-04-03 14:32:15)"
```

**Pros**: Clean  
**Cons**: May diverge from reality if spot moves >5%

---

### **RECOMMENDATION FOR V2**
Implement **Option B + Notification**:

```python
1. Fetch spot at start
2. Run optimizer_v4()
3. If solve takes >15s:
   └─ Check current spot
   └─ If moved >3%:
      └─ Warn: "Spot moved, position may be stale"
   └─ Optionally re-solve

Output: "Optimization valid for spot ±3% from $2000"
```

### Implementation Sketch
```python
async def run_live_pipeline_v2():
    spot_initial = await fetch_spot_price()
    
    start_time = time.time()
    result = solve_portfolio_v4(pkg, params)
    elapsed = time.time() - start_time
    
    if elapsed > 15:
        spot_now = await fetch_spot_price()
        drift = abs(spot_now - spot_initial) / spot_initial
        
        if drift > 0.03:  # >3%
            print(f"⚠️ STALE: Spot moved {drift:.1%} ({spot_initial} → {spot_now})")
            print("Recommend: Re-run optimizer")
        else:
            print(f"✓ Valid: Spot drift {drift:.1%}")
```

---

## V2 DESIGN SUMMARY (No Code Changes Yet)

### Findings
1. ✅ Margin refinement: **correct as-is** (DOES re-solve MILP)
2. ✅ IV stress: **correct as-is** (absolute +25pp shift per spec)
3. ✅ Bid/ask in scenarios: **fixed correctly** (entry cost locked, theta floats)
4. 📝 Output format: **NEW** (list + details)
5. 📝 Relaxation tracking: **NEW** (display which constraints relaxed)
6. 📝 Live spot updates: **NEW** (polling + staleness warnings)
7. 🎯 Library API: **defer to v2.1**

### Next Phase
- Implement output format change
- Add live spot refresh logic
- Update diagnostics for relaxation transparency
- Test with real data (live mode)

### Token Budget
- Research: ~0.5k tokens
- Implementation: ~2-3k tokens (small changes)

---

**END OF RESEARCH**

Ready for implementation plan?
