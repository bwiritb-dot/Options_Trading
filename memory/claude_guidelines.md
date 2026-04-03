---
name: Claude's Professional Development Guidelines
description: Operating principles and decision framework for ETH Options Optimizer development
type: feedback
---

## CORE PRINCIPLE
**Financial correctness > Development speed**

Every decision prioritizes mathematical accuracy and trading reality over convenience.

## DECISION FRAMEWORK FOR TECHNICAL QUESTIONS

When facing 2+ options:
1. **Research** (token-aware):
   - Grep code for clues (cheap)
   - Check spec first
   - Ask user if unclear (free)
2. **Compare**:
   - List pros/cons for each option
   - Include risks and edge cases
   - Show as comparison matrix
3. **Recommend**:
   - Explain the choice
   - Flag assumptions
   - Note if deferring to user

## TOKEN EFFICIENCY RULES

✓ **Cheap sources** (use first):
  - Code inspection (grep)
  - Bash exploration
  - Existing documentation

✗ **Expensive sources** (use last):
  - Web searches
  - Full file reads
  - Multiple exploratory reads

**Before deep work**: Always ask if already answered

## CODE QUALITY STANDARDS

✅ Must have:
- Type hints on all functions
- Docstrings (Args/Returns/Raises)
- Named constants (no magic numbers)
- Error handling for edge cases
- Comments explaining WHY (not WHAT)

✗ Never:
- Guess about financial calculations
- Swallow errors silently
- Modify architecture without confirmation
- Move functions between modules

## TESTING REQUIREMENTS

Each change must include:
- Unit tests for the feature
- Edge case validation
- Integration test (full pipeline)
- Manual verification with real data

## COMMUNICATION TEMPLATE

Before work:
```
📋 PLAN
├─ What: [description]
├─ Why: [business reason]
├─ How: [technical approach, 2-3 steps]
└─ Risks: [edge cases]
```

After work:
```
✅ DONE
├─ Files: [list with line numbers]
├─ Tests: [pass/fail]
└─ Next: [unblocked work]
```

## WHEN IN DOUBT

Priority:
1. Ask the user (cheapest, most accurate)
2. Check the spec (Instructions_Ideas.txt)
3. Inspect code (grep, targeted read)
4. Run tests (understand behavior)
5. Make educated guess + flag it

**Never guess about financial rules.**

## ARCHITECTURAL RULES

The 5-module structure is immutable:
- step1: Data fetching & filtering
- step2: Payoff matrix & Greeks
- step3: MILP solver + relaxation
- step4: Extended MILP (scenarios + IV stress)
- step5: Margin refinement

Each module is independently testable. Do NOT move functions between them.

## LIVE DATA REQUIREMENT

This project uses **real Deribit data**.
- Live mode is CRITICAL
- Test mode is secondary (CI/CD only)
- Default: Always use live data when available

## MODIFICATION REVIEW CHECKLIST

Before committing any change:
- [ ] Spec compliance verified (doesn't break existing behavior)
- [ ] Error handling added (all edge cases)
- [ ] Tests passing (unit + integration)
- [ ] Financial correctness validated
- [ ] No breaking changes (backward compatible)
- [ ] Commit message clear (what + why)
- [ ] No credentials leaked

## RESEARCH APPROACH

For unknowns:
1. Check code/spec first
2. Design comparison if multiple options
3. Recommend with reasoning
4. Implement only when approved

Don't overthink. Simple questions get simple answers. Complex questions get research.

---

**Apply these rules to every task, every decision, every commit.**
