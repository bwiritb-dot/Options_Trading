---
name: ETH Options Optimizer v2 Status
description: Current implementation status, verified findings, and planned changes for v2
type: project
---

## V1 Verification Summary (Completed 2026-04-03)

### What's Correct
- ✅ **Margin refinement**: DOES re-solve MILP (not just update vector) — Correct per spec
- ✅ **IV stress**: Uses absolute +25pp shift (not percentage) — Spec-compliant
- ✅ **Bid/ask in scenarios**: Entry cost locked, theta floats with spot/IV moves — Correct
- ✅ **All 5 modules implemented** and working (13/13 tests pass in synthetic mode)
- ✅ **Live mode operational** (hits Deribit API without auth)

### V1 Limitations
- ❌ Output format: Table format, not the simple list user wants
- ❌ No relaxation transparency: User doesn't see WHICH constraints were relaxed
- ❌ Live spot stale: Optimization assumes spot frozen during 30s solver run
- ❌ Edge case: If all relaxation steps fail → just returns empty, no mitigation

## V2 Design (Approved via RESEARCH_V2_DESIGN.md)

### 3 Changes Required

1. **Output Format** (MEDIUM effort)
   - New output: Simple list format
   - Format: `BUY CALL 1200 3 April 2025`
   - Keep detailed metrics (Greeks, P&L scenarios) in separate section
   - Or: add `--format list|detailed` CLI option

2. **Relaxation Transparency** (SMALL effort)
   - Track which constraints relaxed (Step 1, 2, or 3)
   - Display in output header: `Status: OPTIMAL (relaxed 2/3 — MARGIN_BUDGET × 1.2)`
   - Show which Greeks/PnL changed due to relaxation

3. **Live Spot Refresh** (MEDIUM effort)
   - Fetch spot at start
   - If solver runs >15s: Check current spot mid-run
   - If spot drifted >3%: Warn user "Position may be stale"
   - Output: "Valid for ±3% spot band from $2000"

### Features Deferred to v2.1
- Library API wrapper (clean `__init__.py` interface)
- Multi-position history tracking
- Portfolio rebalancing recommendations

## Findings on Technical Decisions

**Margin refinement (re-solve vs vector-only)**:
- ✅ Keep FULL RE-SOLVE (current implementation)
- Reason: Position must adapt to real Deribit margin rules
- Risk: Converges only if feasible at lower qty, but correct financially

**IV stress shift**:
- ✅ Keep ABSOLUTE +25pp shift (current implementation)
- Reason: Matches spec exactly, represents tail risk properly
- Alternative (percentage shift) rejected — contradicts spec

**Bid/ask in scenarios**:
- ✅ Keep FIXED BID/ASK (current implementation)
- Reason: Entry cost is sunk cost, theta evaluation should float with market
- Interpretation: Trader enters NOW, holds 5 days → scenarios are market moves

**Infeasibility handling**:
- ✅ Current: Auto-relax in order (PNL floor → margin budget → delta)
- ✅ New v2: Display which steps were needed
- Edge case: All 4 attempts fail → return empty with diagnostics (user can loosen manually)

## Code Quality Observations

- All 5 modules are independent and testable ✅
- Type hints present everywhere ✅
- Error handling mostly complete (edge cases for infeasibility handled)
- Comments explain WHY (not WHAT) ✅
- No magic numbers (all constants named) ✅

## Next Steps for Implementation

1. Create `MODIFICATION_LIST_V2.md` with detailed implementation specs
2. Implement output format change (new `print_position_list()` function)
3. Add relaxation tracking to MILPResult dataclass
4. Implement live spot refresh logic in `run_live_pipeline()`
5. Update tests to verify new output format
6. Full end-to-end test with real Deribit data

## Project Context

- **User**: Building ETH options optimizer for Deribit (real trading)
- **Framework**: Python + cvxpy MILP + asyncio
- **Live data**: CRITICAL (real Deribit prices, not simulation)
- **Financial focus**: Correctness > Speed (per spec)
- **Specifications**: Detailed in Instructions_Ideas.txt (Russian language)
