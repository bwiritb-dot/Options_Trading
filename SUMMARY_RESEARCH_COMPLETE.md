# RESEARCH COMPLETE — V2 DESIGN SUMMARY

**Date**: 2026-04-03  
**Time**: Research phase complete, no code modified yet  
**Status**: Ready for implementation phase  

---

## YOUR QUESTIONS — ANSWERS

### 1. Margin Refinement Re-Solve: YES ✅
**Current code: DOES re-solve MILP**

- Fetches real margins → re-solve if over budget
- NOT just vector update (that would be incorrect)
- Correct per spec, already working

### 2. IV Stress Calculation: ABSOLUTE +25pp ✅
**Current code: Correct**

- Shift: mark_iv + 0.25 (e.g., 45% → 70%)
- NOT percentage (e.g., NOT 45% × 1.25)
- Matches spec exactly

### 3. Bid/Ask in Scenarios: FIXED ✅
**Current code: Correct**

- Entry cost locked at current bid/ask
- Theta floats with spot/IV moves
- Interpretation: Trader enters NOW, holds 5 days

### 4. Output Format: CHANGE REQUIRED ❌
**New format requested:**
```
BUY CALL 1200 3 April 2025
SELL PUT 1400 3 April 2025
```
**Status**: Specification created, ready to implement (Modification 1)

### 5. Edge Cases: CLARIFIED ✅
**When MILP infeasible:**
- Step 1: Relax PNL_FLOOR by 20%
- Step 2: Relax MARGIN_BUDGET by 20%
- Step 3: Relax MAX_DELTA by 2x
- Step 4: Return empty with diagnostics

**v2 addition**: Show user WHICH constraints were relaxed

### 6. Margin Refinement Approach: CONFIRMED ✅
**Current**: Full re-solve (Option A)

Why not vector-only (Option B)?
- ❌ Option B: Position would violate real margin requirements
- ✅ Option A: Position adapts to actual constraints
- Risk: Only if budget is unreasonably low → infeasible

### 7. IV Stress Formula: CONFIRMED ✅
**Current**: Absolute shift (Option A)

Why not percentage (Option B)?
- ❌ Option B: Contradicts spec, wrong in calm markets
- ✅ Option A: Spec-compliant, tail risk modeling

### 8. Callable as Library AND CLI: YES ✅
**Current structure**: Already both-capable

- CLI: `python optimizer.py --test`
- Library: `from optimizer_step4 import solve_portfolio_v4`
- v2.1: Add clean wrapper (`optimizer_api.py`)

### 9. Infeasibility Handling: CLARIFIED ✅
**Relaxation** = automatically loosening constraints

- Ordered: floor → budget → delta
- Tracks which step enabled feasibility
- v2: Display in output

### 10. Live Data: CRITICAL ✅
**Your decision**: Real Deribit data required

- Current: Working (live mode operational)
- v2 addition: Spot refresh + staleness warning

---

## 3 MODIFICATIONS FOR V2

### Modification 1: OUTPUT FORMAT (Medium)
**Change**: Simple list format
```
BEFORE:
  Leg  Type  Strike  Expiry      Dir    Qty  Premium   Entry $
   1   CALL  2,100   16 Jan 25   SHORT   -3   $38.50   $115.50

AFTER:
  BUY CALL 2100 16 January 2025
  SELL CALL 2100 16 January 2025
```

**Files**: `optimizer.py` (new function + CLI option)  
**Effort**: ~200 tokens  
**Risk**: Low (additive change)

### Modification 2: RELAXATION TRANSPARENCY (Small)
**Change**: Show user which constraints were relaxed

```
Status: OPTIMAL (relaxed 2/3)

CONSTRAINT RELAXATION:
  • PNL_FLOOR: -400 → -480 USD
  • MARGIN_BUDGET: 5000 → 6000 USD
```

**Files**: `optimizer_step3.py`, `optimizer_step4.py`, `optimizer.py`  
**Effort**: ~300 tokens  
**Risk**: Low (informational only)

### Modification 3: LIVE SPOT REFRESH (Medium)
**Change**: Check if spot drifted >3% during solve, warn if stale

```
✓ Position valid
  Solver took 18.2s, spot drift: -1.2%
```

or

```
⚠️ POSITION STALE
  Solver took 32.1s
  Spot moved $2000 → $2070 (+3.5%)
  Recommend: Re-run optimizer
```

**Files**: `optimizer.py` (timing + refresh check)  
**Effort**: ~250 tokens  
**Risk**: Low (warning only, no functional change)

---

## SUPPORTING DOCUMENTS CREATED

| File | Purpose | Size |
|------|---------|------|
| **SYSTEM_PROMPT_V2.md** | Professional guidelines for future work | Key reference |
| **RESEARCH_V2_DESIGN.md** | Full research details (8 sections) | Detailed analysis |
| **MODIFICATION_LIST_V2.md** | Step-by-step implementation specs | Ready to code |
| **project_state_v2.md** | Saved to memory for future sessions | Context preservation |

---

## CURRENT CODE STATUS

### What's Working (V1) ✅
```
5 Modules:
  ✅ Step 1 (Data): Fetch & filter options
  ✅ Step 2 (Payoff): Matrix + Greeks
  ✅ Step 3 (MILP Base): Solver + relaxation
  ✅ Step 4 (MILP Extended): Scenarios + IV stress
  ✅ Step 5 (Margin): Refinement + re-solve

Testing:
  ✅ 13/13 synthetic tests passing
  ✅ All modules independently testable
  ✅ Live mode works (hits Deribit API)

Code Quality:
  ✅ Type hints everywhere
  ✅ Docstrings with Args/Returns/Raises
  ✅ No magic numbers
  ✅ Error handling for edge cases
  ✅ Financial correctness verified
```

### What's Changing (V2) 📝
```
Output Format:
  ❌ Table → ✅ List format

Relaxation Info:
  ❌ Hidden → ✅ Shown in output

Spot Refresh:
  ❌ None → ✅ Staleness warning

Backward Compat:
  ✅ `--format detailed` for v1 style
  ✅ No breaking changes
```

---

## DECISION RECORD (For Future Reference)

| Question | Answer | Why | Trade-offs |
|----------|--------|-----|------------|
| Re-solve margin? | YES (full) | Correctness | Slower, but accurate |
| IV shift absolute or %? | Absolute +25pp | Spec match | Not multiplicative |
| Bid/ask move with spot? | NO (fixed) | Sunk cost | Theta focus, not bid/ask tightening |
| Output format? | Simple list | User request | Keep detailed metrics separate |
| Show relaxation? | YES | Transparency | More output lines |
| Check spot drift? | YES (>3%) | Reality check | Only if solver slow |
| Library API? | Defer v2.1 | MVP first | Clean up later |
| Live data? | REQUIRED | Critical | Real trading |

---

## NEXT STEPS (IF APPROVED)

### Phase 1: Development (2-3 days)
1. **Day 1**: Implement Modifications 1 & 2
   - New output format
   - Relaxation tracking
   - CLI `--format` option

2. **Day 2**: Implement Modification 3
   - Spot refresh logic
   - Staleness warning
   - Timing wrapper

3. **Day 3**: QA & Testing
   - Full test suite
   - Synthetic + live data
   - Manual validation

### Phase 2: Deployment
1. Merge to `develop`
2. Test on live Deribit data
3. Tag `v2.0-beta`
4. Review output format
5. Merge to `main` → Tag `v2.0`

---

## SYSTEM PROMPT UPDATE

Added **SYSTEM_PROMPT_V2.md**:
- Token efficiency guidelines
- Decision framework for technical questions
- Code standards checklist
- Debugging workflow
- Communication templates
- Architectural rules
- When in doubt, decision matrix

**Key principle**: Financial correctness > speed

---

## TOKEN BUDGET SUMMARY

### Research Phase (COMPLETED)
- Token cost: ~5k (out of 200k budget)
- Breakdown:
  - Code inspection: ~1k
  - Analysis & comparison: ~2k
  - Documentation: ~2k

### Implementation Phase (PENDING)
- Token budget: ~750 tokens (est.)
  - Modification 1: ~200 tokens
  - Modification 2: ~300 tokens
  - Modification 3: ~250 tokens

### Reserve: ~194k tokens remain

---

## READY TO PROCEED?

### ✅ Complete
- [x] Understand project (5 modules working)
- [x] Verify spec vs implementation (all v1 features correct)
- [x] Answer 10 clarifying questions
- [x] Create decision matrix
- [x] Design v2 modifications (3 changes)
- [x] Create implementation specs
- [x] Document guidelines for future work

### ⏳ Pending User Decision
- [ ] Approve v2 modifications?
- [ ] Start implementation (Day 1)?
- [ ] Order of modifications (1 → 2 → 3)?
- [ ] Any changes to proposed specs?

### 📋 Command to Start
```bash
# When ready, respond with:
# "Start v2 implementation" 
# or 
# "Make changes to Modification X first"
```

---

**Status**: Awaiting your approval to proceed with code changes.

All research complete, system prompt updated, modifications specified, ready to build.

