# V2 MODIFICATION LIST — Implementation Specifications

**Target**: Upgrade from v1.0 → v2.0  
**Scope**: 3 modifications + testing  
**Total effort**: ~2-3 days development + QA  

---

## MODIFICATION 1: OUTPUT FORMAT CHANGE

### Current Status (v1)
```
OPTIMAL POSITION [Solver: GLPK_MI | Status: OPTIMAL]
────────────────────────────────────────────────────
  Leg  Type  Strike  Expiry      Dir    Qty  Premium   Entry $
   1   CALL  2,100   16 Jan 25   SHORT   -3   $38.50   $115.50
   2   CALL  2,200   16 Jan 25   LONG    +3   $11.20   -$33.60
   3   PUT   1,900   16 Jan 25   SHORT   -3   $45.20   $135.60
   4   PUT   1,800   16 Jan 25   LONG    +3   $12.80   -$38.40

NET GREEKS
────────────────────────────────────────────────────
  Theta (USD/day):  +$142.30
  Delta:             -0.08
  [... more metrics]
```

### Target Status (v2)
```
OPTIMAL POSITION — 4 Legs
─────────────────────────
BUY CALL 2100 16 January 2025
BUY PUT 1800 16 January 2025
SELL CALL 2100 16 January 2025
SELL PUT 1900 16 January 2025

NET GREEKS
─────────────────────────
Theta (USD/day): +$142.30
Theta (5-day hold): +$711.50
Delta: -0.08 (limit ±0.15)
Gamma: -0.003 (limit ≥-0.008)
Vega (USD/1%): -$220.40 (limit ±$600)

P&L SCENARIOS
─────────────────────────
[Keep current detailed format]

RISK METRICS
─────────────────────────
[Keep current format]
```

### Implementation Details

**File**: `optimizer.py`, Lines 125-450 (currently `print_final_report()`)

**Changes**:
1. Add new function: `print_position_list(result, pkg, spot_price)`
   ```python
   def print_position_list(
       result: ExtendedMILPResult,
       pkg: PayoffPackage,
       spot_price: float,
   ) -> None:
       """Print simple list format: BUY/SELL TYPE STRIKE DATE"""
       
       print(f"\nOPTIMAL POSITION — {len(result.active_legs)} Legs")
       print("─" * 50)
       
       for i in result.active_legs:
           inst = pkg.instruments[i]
           qty = result.x[i]
           
           action = "BUY" if qty > 0 else "SELL"
           opt_type = inst.option_type.upper()
           strike = inst.strike
           
           # Date format: "3 January 2025" (full month name)
           date_str = _format_expiry_longform(inst.expiry_ts)
           
           print(f"{action} {opt_type} {strike:,.0f} {date_str}")
   ```

2. Keep `print_final_report()` but restructure:
   - POSITION section: Call new `print_position_list()`
   - GREEKS section: Simplify (remove "Premium", "Entry $" columns)
   - P&L SCENARIOS: Keep as-is (no change)
   - RISK METRICS: Keep as-is (no change)

3. Add CLI option: `--format list|detailed`
   - Default: `list` (simple format)
   - Optional: `detailed` (v1 table format for power users)

4. Add date formatting helper:
   ```python
   def _format_expiry_longform(expiry_ts: int) -> str:
       """Convert timestamp to "3 January 2025" format."""
       from datetime import datetime
       dt = datetime.fromtimestamp(expiry_ts / 1000, tz=timezone.utc)
       return dt.strftime("%d %B %Y")  # "16 January 2025"
   ```

### Testing
- [ ] Synthetic data: Verify list format is readable
- [ ] Live data: Run with real Deribit options, check format
- [ ] Edge cases: Empty position, single leg, 10+ legs

### Token Cost: ~200 tokens

---

## MODIFICATION 2: RELAXATION TRANSPARENCY

### Current Status (v1)
Output shows:
```
Status: OPTIMAL (relaxed 2/3)
```
But doesn't explain WHICH constraints were relaxed.

### Target Status (v2)
Output shows:
```
Status: OPTIMAL (relaxed 2/3)

CONSTRAINT RELAXATION:
  Step 1: PNL_FLOOR relaxed -400 → -480 USD (20%)
  Step 2: MARGIN_BUDGET relaxed 5000 → 6000 USD (20%)
  [No further relaxation needed]
```

### Implementation Details

**Files to modify**:

1. `optimizer_step3.py`, Line 137 (MILPResult dataclass)
   - Add fields:
     ```python
     @dataclass
     class MILPResult:
         ...
         relaxation_step: int = 0                    # 0-3
         relaxation_details: list[str] = field(...)  # NEW: [description]
         original_params: Optional[MILPParams] = None  # NEW: track original
     ```

2. `optimizer_step4.py`, Line 671 (ExtendedMILPResult dataclass)
   - Inherit same fields from MILPResult
   - May not need additional changes

3. `optimizer_step3.py`, Lines 546-600 (relax_and_retry function)
   - Track relaxation steps taken:
     ```python
     def relax_and_retry(...):
         relaxation_log = []
         
         for step in range(1, 4):
             relaxed_params = _relax_params(params, step)
             
             # NEW: Log what changed
             if step == 1:
                 old = params.pnl_floor
                 new = relaxed_params.pnl_floor
                 relaxation_log.append(f"PNL_FLOOR: {old} → {new} USD")
             elif step == 2:
                 old = params.margin_budget
                 new = relaxed_params.margin_budget
                 relaxation_log.append(f"MARGIN_BUDGET: ${old} → ${new}")
             elif step == 3:
                 old = params.max_delta
                 new = relaxed_params.max_delta
                 relaxation_log.append(f"MAX_DELTA: {old:.3f} → {new:.3f}")
             
             result = solve_portfolio(...)
             if result.status == OPTIMAL:
                 result.relaxation_step = step
                 result.relaxation_details = relaxation_log
                 return result
     ```

4. `optimizer.py`, Lines 180-185 (print_final_report)
   - Add relaxation section:
     ```python
     if result.relaxation_step > 0 and result.relaxation_details:
         print(f"\nCONSTRAINT RELAXATION (Step {result.relaxation_step}/3)")
         print("─" * 50)
         for detail in result.relaxation_details:
             print(f"  • {detail}")
     ```

### Testing
- [ ] Synthetic: Force infeasibility, verify relaxation log
- [ ] Verify Step 1, Step 2, Step 3 all logged correctly
- [ ] Check output formatting (bullets, alignment)

### Token Cost: ~300 tokens

---

## MODIFICATION 3: LIVE SPOT PRICE REFRESH

### Current Status (v1)
Spot price fetched once, used throughout optimization (~30s):
```python
spot_initial = await fetch_spot_price()  # T=0
result = solve_portfolio_v4(pkg, params)  # T=0-30s, spot unchanged
print_final_report(result, ...)  # T=30s, but spot may have moved
```

**Problem**: If ETH moves significantly (±5%+), position recommendation may be stale.

### Target Status (v2)
```python
spot_initial = await fetch_spot_price()  # T=0
start_time = time.time()

result = solve_portfolio_v4(pkg, params)  # T=0-30s

elapsed = time.time() - start_time

if elapsed > 15 seconds:  # Only check if took long
    spot_now = await fetch_spot_price()
    drift_pct = abs(spot_now - spot_initial) / spot_initial
    
    if drift_pct > 0.03:  # >3%
        print(f"⚠️  STALE POSITION: Spot moved {drift_pct:.1%}")
        print(f"    {spot_initial} → {spot_now}")
        print(f"    Recommend: Re-run optimizer")
    else:
        print(f"✓ Position valid (spot drift: {drift_pct:.1%})")
```

### Implementation Details

**File**: `optimizer.py`, Lines 355-450 (run_live_pipeline function)

**Changes**:

1. Wrap solver call with timing:
   ```python
   async def run_live_pipeline(params: ExtendedMILPParams) -> None:
       """..."""
       spot_initial = await fetch_spot_price()
       
       # NEW: Timing wrapper
       solver_start = time.time()
       
       # Existing: build pkg and solve
       pkg = await build_payoff_package(...)
       result = await solve_portfolio_v4(pkg, params)
       
       solver_elapsed = time.time() - solver_start
       
       # NEW: Check spot refresh (only if took time)
       spot_drift_result = None
       if solver_elapsed > 15:  # Only if >15s
           spot_now = await fetch_spot_price()
           drift = abs(spot_now - spot_initial) / spot_initial
           
           spot_drift_result = {
               "elapsed_seconds": solver_elapsed,
               "spot_initial": spot_initial,
               "spot_final": spot_now,
               "drift_pct": drift,
               "is_stale": drift > 0.03,
           }
       
       # Existing: refinement + output
       refinement = await refine_margins(...)
       print_final_report(
           result, refinement, pkg, stats, spot_initial, params,
           spot_drift=spot_drift_result  # NEW parameter
       )
   ```

2. Update `print_final_report()` to accept `spot_drift` parameter:
   ```python
   def print_final_report(
       ...,
       spot_drift: Optional[dict] = None,  # NEW
   ) -> None:
       """..."""
       # Existing output...
       
       # NEW: Spot drift warning
       if spot_drift and spot_drift["is_stale"]:
           print(f"\n⚠️  POSITION STALE")
           print(f"────────────────────────────────────────")
           print(f"Solver took {spot_drift['elapsed_seconds']:.1f}s")
           print(f"Spot moved {spot_drift['spot_initial']} → ${spot_drift['spot_final']} "
                 f"({spot_drift['drift_pct']:+.1%})")
           print(f"\nRECOMMENDATION: Re-run optimizer")
       elif spot_drift:
           print(f"\n✓ Position valid")
           print(f"Solver took {spot_drift['elapsed_seconds']:.1f}s, "
                 f"spot drift: {spot_drift['drift_pct']:+.1%}")
   ```

3. Add import:
   ```python
   import time  # At top of optimizer.py
   ```

### Testing
- [ ] Run with synthetic data, verify timing + output
- [ ] Live data: With slow API, verify >15s check triggers
- [ ] Edge case: Spot moves >3%, verify warning shown
- [ ] Edge case: Fast solver (<15s), skip check (no extra API call)

### Token Cost: ~250 tokens

---

## MODIFICATION 4: TESTING & VALIDATION

### Test Suite Changes

**New test cases** (add to `optimizer.py` or each step file):

```python
def test_output_format_list():
    """Position list format is readable and complete."""
    result = run_synthetic(--test)
    # Verify output contains: "BUY CALL 1200 3 April 2025"
    # NOT "table with columns"

def test_relaxation_transparency():
    """Relaxation steps logged in output."""
    # Force infeasibility → trigger relaxation
    params = MILPParams(margin_budget=500)  # Unreasonably low
    result = solve_portfolio_v4(pkg, params)
    assert result.relaxation_step >= 1, "Should relax"
    assert len(result.relaxation_details) > 0, "Should log details"

def test_live_spot_drift():
    """Spot refresh only triggers if solver took >15s."""
    # Mock solver to take 20s
    # Verify spot_drift_result is populated
    # Verify output shows warning if drift >3%

def test_output_exact_format():
    """Output matches expected format (list, not table)."""
    result = run_synthetic()
    output = capture_output(print_final_report, result)
    assert "BUY" in output or "SELL" in output
    assert "CALL" in output or "PUT" in output
    assert not ("Premium" in output)  # v1 format should be gone
```

### Manual Testing Checklist
- [ ] Run `python optimizer.py --test` (synthetic)
- [ ] Run `python optimizer.py --test --verbose` (with details)
- [ ] Run `python optimizer.py --spot 2000` (live data, if available)
- [ ] Verify output format matches spec
- [ ] Verify relaxation logged (if any)
- [ ] Verify spot drift warning (if slow)
- [ ] Check all Greeks are present and sensible
- [ ] Check P&L scenarios cover all 35 price points (or representative subset)

---

## SUMMARY: V2 MODIFICATIONS

| # | Feature | Files | Effort | Risk | Testing |
|---|---------|-------|--------|------|---------|
| 1 | Output format (list) | `optimizer.py` | Medium | Low | Moderate |
| 2 | Relaxation transparency | `optimizer_step3/4.py`, `optimizer.py` | Small | Low | Easy |
| 3 | Spot refresh + stale warning | `optimizer.py` | Medium | Low | Moderate |
| 4 | Testing & validation | All files | Small | Low | High |

**Total effort**: ~2-3 days (dev + testing)  
**Risk**: Low (changes are additive, no architectural changes)  
**Breaking changes**: None (backward compatible with --format flag)

---

## IMPLEMENTATION ORDER

### Phase 1: Foundation (Day 1)
1. Create new `print_position_list()` function
2. Add `--format` CLI option
3. Add date formatting helper
4. Test with synthetic data

### Phase 2: Transparency (Day 1-2)
1. Extend MILPResult dataclass with relaxation fields
2. Update relax_and_retry() to log details
3. Update print_final_report() to show relaxation
4. Test with forced infeasibility

### Phase 3: Live Data (Day 2)
1. Add timing wrapper around solve_portfolio_v4()
2. Add spot price refresh logic
3. Update print_final_report() to show drift warning
4. Test with real Deribit data (time slow solver to trigger check)

### Phase 4: QA (Day 3)
1. Full end-to-end tests (synthetic + live)
2. Edge case validation
3. Output format review
4. Manual testing checklist
5. Commit to main branch

---

## ROLLOUT PLAN

**Backward compatibility**: ✅ Yes
- Default: `--format list` (new)
- Optional: `--format detailed` (v1 style)
- Spot drift: Warning only (doesn't block)
- Relaxation log: Informational (doesn't affect position)

**Deployment**:
1. Merge to `develop` branch
2. Test on staging (real Deribit data)
3. Tag as `v2.0-beta`
4. Merge to `main` when verified
5. Tag as `v2.0`

---

**Ready to start implementation?**

