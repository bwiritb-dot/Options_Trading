# WEEKLY DECISION TOOL — Complete Development Specification
# Version: MVP 1.0
# Last Updated: 2026-04-05

---

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 0: AGENT BEHAVIORAL INSTRUCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

## 0.1 How You Must Work

1. **READ THIS ENTIRE DOCUMENT** before writing any code. Do not start coding
   until you understand every section.

2. **Build ONE module at a time**, in the order specified in Section 5.
   After each module: write it, test it, confirm it works, then move to the next.

3. **If ANYTHING is unclear — ASK THE USER.** Do not guess. Do not assume.
   Say: "I don't understand X in Section Y. Can you clarify?"

4. **Never skip math.** If a formula is given, implement it EXACTLY as written.
   Do not "simplify" or "optimize" the math. The formulas are carefully designed.

5. **Use the existing codebase.** Files `optimizer_step1.py` through
   `optimizer_step5.py` and `optimizer.py` already exist. READ THEM FIRST.
   Reuse their dataclasses, functions, and patterns. Do not rewrite what exists.

6. **Constants go at the top of each file** in a clearly labeled section.
   No magic numbers anywhere in the code. Every number must be a named constant
   or a parameter in a dataclass.

7. **Every function must have**: type hints, a docstring with Args/Returns,
   and at least one unit test (at the bottom of the file, in `if __name__ == "__main__"`).

8. **Python 3.11+**. Use dataclasses, not dicts. Use `numpy` for matrices.
   Use `cvxpy` for MILP. Use `aiohttp` for HTTP. No pandas (too heavy for this).

9. **Error handling**: Only at system boundaries (API calls, file I/O, user input).
   Internal functions trust their inputs. Do not add defensive checks everywhere.

10. **DO NOT create any .md files, README files, or documentation files.**
    The only output is Python code files.

11. **When you need to research something** (e.g., Deribit API response format),
    use web search or read the existing code. Do not guess API responses.

12. **Test with real Deribit data** where possible (public endpoints, no auth needed).
    Use `https://test.deribit.com/api/v2` for testnet during development.

## 0.2 Code Style Rules

- 4-space indentation
- Line length: 100 chars max
- Imports: stdlib → third-party → local, separated by blank lines
- Constants: UPPER_SNAKE_CASE
- Classes: PascalCase
- Functions: lower_snake_case
- File encoding: UTF-8
- All comments and docstrings in ENGLISH (the existing codebase has some Russian
  comments — ignore that pattern, write in English)
- Use `logging` module (not print) for debug output. `print()` only for final user output.

## 0.3 When To Ask The User

Ask the user when:
- A formula seems wrong or contradictory
- You need to make a design choice not covered in this spec
- A test fails and you don't understand why after investigating
- The Deribit API returns unexpected data
- You need to change an existing file in a way that might break other modules

Do NOT ask the user when:
- You need to look up Deribit API docs (just research it)
- You need to decide variable names or internal structure (just decide)
- You need to choose between equivalent implementation approaches (just pick one)

---

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: WHAT THIS TOOL DOES (THE STRATEGY — EXPLAINED SIMPLY)
# ═══════════════════════════════════════════════════════════════════════════════

## 1.1 The Business

A user buys "challenge accounts" from crypto prop trading firms. These accounts
let you trade ETH (Ethereum) with the firm's money. The rules:

- You pay a FEE (e.g., $700) to buy the account
- The account has a SIZE (e.g., $100,000)
- You must hit a PROFIT TARGET (e.g., 12%) to PASS the challenge
- If your total losses exceed MDD% (Max Drawdown, e.g., 5%), you FAIL and lose the account
- If your losses on ANY SINGLE DAY exceed DDD% (Daily Drawdown, e.g., 3%), you FAIL that day's
  trades get closed (but the account survives — you just can't trade more that day)
- Once you pass, you get a "funded account" and can extract PAYOUTS

**CRITICAL**: The payout amount = Account Size × MDD% × Payout Split.
NOT the profit target. The profit target is just what you must reach to pass.
Example: $100,000 × 5% × 80% = $4,000 payout.

After payout, you keep the funded account and repeat the cycle.

## 1.2 The Insurance Concept

Separately, on Deribit (a crypto options exchange), the user builds an OPTIONS
PORTFOLIO that generates income (theta/time decay) regardless of what happens
on the prop firm side.

The COMBINED system must satisfy:
- **End-state guarantee**: After each cycle completes, total money spent ≤ total money received.
  You either have profit, OR you still have a surviving funded account (which has value).
- **Account survival**: The prop firm account must not breach MDD or DDD at any point.
  If it does, the account is lost and the money spent on it is gone.
- **Deribit must be theta-positive in flat markets**: If ETH price stays flat for a week,
  the Deribit options portfolio must make money (not lose money).

## 1.3 What The Tool Computes Every Sunday

INPUT:
- A weekly ETH forecast (price ranges with probabilities, daily sentiment, daily volatility)
- Live Deribit options data (all available ETH options with prices, Greeks, IV)
- A list of current prop firm accounts (each with its own rules, balances, existing positions)
- Optionally: one NEW account the user is considering buying

OUTPUT:
- For each account: the optimal set of Deribit options to buy/sell (exact instruments, quantities)
- For each account: the recommended direction (long or short ETH) on the prop firm
- A comparison table ranking all accounts by expected profit and ROI-over-time
- A recommendation: which play to execute, or "do nothing this week"
- Exact Deribit instrument names and quantities to execute

## 1.4 Key Terms (You MUST understand these — they are DIFFERENT things)

| Term | What It Is | Where It Applies |
|------|-----------|-----------------|
| DDD (Daily Drawdown) | Max loss allowed in a single day. Breach = trades closed for that day. Risk only HALF of DDD per trade to prevent slippage breach. | Prop firm only |
| MDD (Max Drawdown) | Max total cumulative loss. Breach = account terminated forever. | Prop firm only |
| Profit Target | The % you must reach to PASS the challenge. NOT used for payout calculation. | Challenge phases only |
| Payout | Cash extracted from funded account. = Size × MDD% × Split. | Funded phase only |
| Payout Split | % of payout you keep (e.g., 80%). Rest goes to firm. | Funded phase only |
| Strike | The price at which an option can be exercised. | Deribit options only |
| IV (Implied Volatility) | Market's expectation of future volatility. Higher = options more expensive. | Deribit options only |
| Theta | Daily time decay of an option's value. Positive theta = you earn money each day. | Deribit options only |
| Delta | How much option price changes per $1 move in ETH. | Deribit options only |
| ATR | Average True Range — typical daily price movement in USD. ~$180 for ETH currently. | Both |
| Iron Condor | Selling a put spread + call spread. Profits if price stays in a range. Theta-positive. | Deribit structure |

---

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: DATA MODELS
# ═══════════════════════════════════════════════════════════════════════════════

## 2.1 Prop Firm Account Configuration

```python
@dataclass
class PropFirmRules:
    """Rules specific to one prop firm plan. Every field is configurable per firm."""
    firm_name: str                    # e.g., "UpScale"
    plan_name: str                    # e.g., "1-Step Pro"
    account_size: float               # USD, e.g., 100_000
    fee: float                        # USD, cost to buy the challenge, e.g., 700

    # Phases: list of phase configs. Funded phase is the LAST entry.
    phases: list["PhaseConfig"]

    # Payout
    payout_split: float               # 0.80 = you keep 80%
    payout_delay_days: int = 1        # days until cash arrives after requesting payout

    # DDD/MDD measurement
    ddd_uses_equity: bool = True      # True = balance + unrealized P&L. False = closed trades only.
    mdd_is_trailing: bool = False     # True = trailing from peak. False = static from start.

    # Trading
    close_reopen_cost: float = 0.0    # USD per round-trip close+reopen on prop firm
    slippage_pct: float = 0.0025      # 0.25% slippage assumption


@dataclass
class PhaseConfig:
    """Configuration for one phase (challenge step or funded)."""
    phase_name: str                   # "Challenge Phase 1", "Challenge Phase 2", "Funded"
    profit_target_pct: float          # e.g., 0.12 for 12%. For funded phase: this is MDD%.
    mdd_pct: float                    # e.g., 0.05 for 5%
    ddd_pct: float                    # e.g., 0.03 for 3%
    min_trading_days: int = 3         # minimum days with open positions before advancing
    is_funded: bool = False           # True only for the funded phase


@dataclass
class AccountState:
    """Current state of one prop firm account the user owns."""
    rules: PropFirmRules
    current_phase_index: int          # which phase we're in (0-indexed)
    current_balance: float            # current balance in USD (includes unrealized if equity-based)
    starting_balance: float           # balance at start of current phase
    cumulative_insurance_cost: float  # total spent on Deribit insurance for this account so far
    cumulative_fees: float            # total challenge fees paid (may span multiple accounts if retried)
    days_traded_this_phase: int       # how many days with positions opened in current phase
    existing_deribit_positions: list  # list of currently held Deribit positions (can be empty)
    direction: str | None = None      # "long" | "short" | None (if no prop firm position open)

    @property
    def current_phase(self) -> PhaseConfig:
        return self.rules.phases[self.current_phase_index]

    @property
    def account_value(self) -> float:
        """What this account is 'worth' = total payouts received - total spent.
        Negative means we've spent more than received (normal during challenge)."""
        total_spent = self.cumulative_fees + self.cumulative_insurance_cost
        # During challenge: no payouts yet, so value is negative
        return -total_spent

    @property
    def payout_amount(self) -> float:
        """USD received when a funded payout is requested."""
        return self.rules.account_size * self.current_phase.mdd_pct * self.rules.payout_split

    @property
    def ddd_limit_usd(self) -> float:
        """Max daily loss in USD."""
        return self.starting_balance * self.current_phase.ddd_pct

    @property
    def safe_risk_per_trade(self) -> float:
        """Risk only half of DDD minus slippage to prevent breach."""
        return (self.ddd_limit_usd / 2) - (self.starting_balance * self.rules.slippage_pct)

    @property
    def mdd_remaining_usd(self) -> float:
        """How much total drawdown is left before account dies."""
        if self.rules.mdd_is_trailing:
            # Trailing: measured from peak balance
            # For simplicity, assume current_balance IS the peak (conservative)
            return self.current_balance * self.current_phase.mdd_pct
        else:
            # Static: measured from starting balance
            return self.starting_balance * self.current_phase.mdd_pct - (
                self.starting_balance - self.current_balance
            )
```

## 2.2 Weekly Forecast Input

```python
@dataclass
class DailyForecast:
    """Forecast for a single day (Monday through Friday)."""
    day_name: str                     # "Monday", "Tuesday", etc.
    sentiment: str                    # "bullish" | "bearish" | "neutral"
    sentiment_confidence: float       # 0.0 to 1.0 (e.g., 0.68 = 68% confident)
    expected_volatility_usd: float    # expected daily range in USD (from ATR)
    macro_events: list[str]           # e.g., ["FOMC Minutes", "CPI Release"]


@dataclass
class PriceRangeBucket:
    """One probability bucket for Friday close price."""
    range_low: float                  # USD, e.g., 1800
    range_high: float                 # USD, e.g., 1900
    probability: float                # e.g., 0.25 = 25%


@dataclass
class WeeklyForecast:
    """Complete weekly intelligence report, parsed into structured data."""
    report_date: str                  # ISO date, e.g., "2026-04-05"
    eth_spot_at_report: float         # ETH/USD price when report was written
    overall_direction: str            # "bullish" | "bearish" | "neutral"
    overall_confidence: float         # 0.0 to 1.0
    daily_forecasts: list[DailyForecast]  # Monday through Friday (5 entries)
    friday_price_ranges: list[PriceRangeBucket]  # probability distribution for Friday close
    weekly_atr_usd: float             # average true range in USD (e.g., 180)
```

## 2.3 Scenario Paths (Generated From Forecast)

```python
@dataclass
class DayPoint:
    """One day's simulated state within a scenario path."""
    day_index: int                    # 0=Monday, 1=Tuesday, ..., 4=Friday
    open_price: float                 # USD
    high_price: float                 # USD (needed for short DDD check)
    low_price: float                  # USD (needed for long DDD check)
    close_price: float                # USD
    iv_atm: float                     # ATM implied volatility (annualized, e.g., 0.65)


@dataclass
class ScenarioPath:
    """One complete weekly price path (Mon-Fri) with metadata."""
    path_id: int
    description: str                  # e.g., "Bearish crash Mon-Wed, recovery Thu-Fri"
    probability: float                # weight of this path (all paths sum to 1.0)
    days: list[DayPoint]              # 5 entries (Mon-Fri)
    friday_close: float               # shortcut: days[-1].close_price
    is_adverse: bool                  # True if this is a "bad week" scenario
```

## 2.4 MILP Decision Output

```python
@dataclass
class OptionLeg:
    """One leg of the recommended options portfolio."""
    instrument_name: str              # Deribit instrument name, e.g., "ETH-11APR26-2200-C"
    action: str                       # "buy" | "sell"
    quantity: int                     # number of contracts
    price: float                      # execution price in ETH (bid for sell, ask for buy)
    price_usd: float                  # price × spot
    delta: float                      # per-contract delta
    theta_usd_day: float              # per-contract theta in USD/day
    iv: float                         # implied volatility


@dataclass
class PlayRecommendation:
    """Complete recommendation for one prop firm account."""
    account: AccountState
    direction: str                    # "long" | "short" — direction on prop firm
    option_legs: list[OptionLeg]      # Deribit positions to open
    total_insurance_cost_usd: float   # net cost of the options portfolio
    expected_profit_usd: float        # probability-weighted expected profit
    worst_case_profit_usd: float      # profit in the worst scenario
    best_case_profit_usd: float       # profit in the best scenario
    roi_per_day: float                # expected_profit / total_cost / days
    portfolio_theta_usd_day: float    # total daily theta income
    portfolio_delta: float            # net portfolio delta
    days_to_target: float             # estimated days to hit profit target
    should_execute: bool              # True if this play is profitable, False if "do nothing"


@dataclass
class WeeklyDecision:
    """Final output: ranked plays across all accounts."""
    timestamp: str
    spot_price: float
    forecast_summary: str
    plays: list[PlayRecommendation]   # sorted by roi_per_day descending
    recommended_play_index: int       # index into plays[] for the best play
    do_nothing_ev: float              # EV of not trading at all this week
```

---

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: MATHEMATICS — EXACT FORMULAS
# ═══════════════════════════════════════════════════════════════════════════════

## 3.1 Prop Firm Economics

### Payout Per Cycle
```
payout_usd = account_size × mdd_pct × payout_split
```
Example: $100,000 × 0.05 × 0.80 = $4,000

### Account Value (How Much Is A Surviving Account Worth)
```
account_value = -cumulative_cost
where cumulative_cost = sum(all_fees_paid) + sum(all_insurance_costs_paid) - sum(all_payouts_received)
```
If cumulative_cost is negative (payouts > costs), account value is positive (free account + profit).
If cumulative_cost is positive (costs > payouts), account value = -cumulative_cost (debt to recover).

### DDD Safety
```
safe_risk_per_trade = (account_size × ddd_pct / 2) - (account_size × slippage_pct)
```
Example: ($100,000 × 0.03 / 2) - ($100,000 × 0.0025) = $1,500 - $250 = $1,250

The "÷ 2" is because: you open a trade risking $1,250. If it hits stop-loss,
you've lost $1,250 (half DDD). You can then REOPEN a new trade in whatever
direction the optimizer says is best. If that also stops out (worst case),
you've lost $2,500 + $250 slippage = $2,750, which is still under DDD ($3,000).

### Prop Firm P&L On A Path
For each scenario path, for each day:
```
# If LONG ETH on prop firm:
prop_pnl_long[day] = (close_price[day] - entry_price) × position_size_eth
ddd_breach_long[day] = (low_price[day] - entry_price) × position_size_eth < -ddd_limit

# If SHORT ETH on prop firm:
prop_pnl_short[day] = (entry_price - close_price[day]) × position_size_eth
ddd_breach_short[day] = (entry_price - high_price[day]) × position_size_eth < -ddd_limit
```

**IMPORTANT**: For LONG positions, the DANGER is the LOW price (intraday dip).
For SHORT positions, the DANGER is the HIGH price (intraday pump).
Each DayPoint must carry BOTH high and low.

### Close/Reopen Simulation Per Path Per Day
```
For each day d in path:
    Check if intraday move breaches DDD (using high/low):
        LONG:  if (low[d] - entry) × size < -ddd_limit → STOP OUT at entry - ddd_limit/size
        SHORT: if (entry - high[d]) × size < -ddd_limit → STOP OUT at entry + ddd_limit/size

    If stopped out:
        realized_loss += ddd_limit  (or half-DDD if using half-risk model)
        remaining_ddd_today = ddd_limit - realized_loss_today
        if remaining_ddd_today > safe_risk_per_trade:
            REOPEN in direction optimizer chooses (could be opposite!)
            new_entry = close[d] at stop-out moment (approximation)
        else:
            DONE for today, no more trades

    Check cumulative MDD:
        if total_losses > mdd_limit → ACCOUNT DEAD, path ends here
```

## 3.2 Black-Scholes Pricing (For Mid-Week Option Valuation)

We need to price options at any (price, IV, time) point during the week,
not just at expiry. This is how we know what the Deribit portfolio is worth
on Tuesday if ETH is at $1,900 with IV at 85%.

```python
def bs_price(S, K, T, sigma, r, option_type):
    """
    Black-Scholes European option price.

    Args:
        S:     spot price (USD)
        K:     strike price (USD)
        T:     time to expiry (YEARS, not days)
        sigma: implied volatility (annualized, e.g., 0.65 for 65%)
        r:     risk-free rate (annualized, e.g., 0.05)
        option_type: "call" or "put"

    Returns:
        Option price in USD
    """
    if T <= 0:
        # At expiry: intrinsic value only
        if option_type == "call":
            return max(S - K, 0)
        else:
            return max(K - S, 0)

    d1 = (ln(S/K) + (r + σ²/2) × T) / (σ × √T)
    d2 = d1 - σ × √T

    if option_type == "call":
        price = S × N(d1) - K × exp(-r×T) × N(d2)
    else:  # put
        price = K × exp(-r×T) × N(-d2) - S × N(-d1)

    return price
```

Where N(x) = standard normal CDF = (1 + erf(x/√2)) / 2

**IMPORTANT**: Deribit prices are in ETH, not USD.
To convert: `price_eth = price_usd / S` (divide by spot).

## 3.3 IV Trajectory Model

When ETH price moves, IV changes. This model gives us IV at any point in a scenario path.

```python
def iv_at_point(base_iv, price_change_pct):
    """
    Estimate IV given a price move from the starting point.

    Args:
        base_iv:          current ATM IV (e.g., 0.65)
        price_change_pct: % change from current price (e.g., -0.05 for 5% drop)

    Returns:
        Estimated IV (annualized)
    """
    if price_change_pct < 0:  # price dropped
        # Crashes increase IV (asymmetric, nonlinear)
        beta = 2.0   # vol points per 1% decline
        gamma = 20.0  # convexity on downside
        move = abs(price_change_pct)
        delta_iv = (beta * move + gamma * move * move) / 100  # convert vol points to decimal
    else:  # price rallied
        # Rallies decrease IV (more linear)
        beta = 1.0
        gamma = 5.0
        move = abs(price_change_pct)
        delta_iv = -(beta * move + gamma * move * move) / 100

    new_iv = base_iv + delta_iv

    # Apply mean reversion toward median (65% baseline)
    IV_MEDIAN = 0.65
    MR_SPEED = 0.14  # daily mean reversion speed (half-life ~5 days)
    new_iv = new_iv + MR_SPEED * (IV_MEDIAN - new_iv)

    # Clamp to realistic range
    return max(0.35, min(2.00, new_iv))
```

**For each day in each scenario path**, compute:
```
price_change_pct = (day.close_price - spot_at_report) / spot_at_report
day.iv_atm = iv_at_point(current_iv, price_change_pct)
```

## 3.4 Deribit Portfolio Valuation At Any Point

For a portfolio of options (the "insurance"), compute its value at any (day, path) point:

```
portfolio_value(day, path) = Σ over all legs:
    leg.quantity × leg.direction × bs_price(
        S = path.days[day].close_price,
        K = leg.strike,
        T = (leg.expiry - current_date - day) / 365,
        sigma = iv_at_strike(path.days[day].iv_atm, leg.strike, S),
        r = 0.05,
        option_type = leg.type
    )

Where:
    leg.direction = +1 if bought, -1 if sold
    iv_at_strike adjusts ATM IV for the smile/skew (see Section 3.5)
```

**Entry cost** (what we paid/received to open the portfolio):
```
entry_cost = Σ over all legs:
    if leg.action == "buy":
        leg.quantity × leg.ask_price × spot  # we pay ask
    else:  # sell
        -leg.quantity × leg.bid_price × spot  # we receive bid (negative cost)
```

**Portfolio P&L at (day, path)**:
```
pnl_deribit(day, path) = portfolio_value(day, path) - entry_cost
```

## 3.5 IV Smile/Skew Adjustment

Options away from ATM have different IVs. OTM puts are more expensive (higher IV).

```
iv_at_strike(atm_iv, strike, spot) =
    atm_iv + skew_slope × (0.5 - delta_approx) + smile_curve × (0.5 - delta_approx)²

Where:
    delta_approx = rough BS delta (just for positioning on the smile, not exact)
    skew_slope = -0.08 (normal) to -0.20 (stress)
    smile_curve = 0.15

Simpler approximation (USE THIS for MVP):
    moneyness = ln(strike / spot)
    iv_at_strike = atm_iv × (1 + 0.1 × moneyness + 0.3 × moneyness²)
```

The simpler approximation is good enough for the MVP. It makes OTM puts and
OTM calls more expensive than ATM, with puts getting a slight extra premium.

## 3.6 Combined P&L (The End-State Constraint)

For each scenario path p, the combined P&L at the END of the week is:

```
combined_pnl(p) = prop_pnl(p, friday) + deribit_pnl(p, friday) - cumulative_cost

Where:
    prop_pnl(p, friday) = profit/loss from prop firm trading on this path
    deribit_pnl(p, friday) = portfolio_value(friday, p) - entry_cost
    cumulative_cost = all prior fees + all prior insurance costs
```

**THE CONSTRAINT**: For ALL paths p:
```
combined_pnl(p) ≥ -account_value
```
Where `account_value` = total spent - total received (see 3.1).

This means: in the worst case, we either break even overall, OR we still have
a surviving funded account worth at least what we've spent.

**For non-funded phases (challenge)**: The prop firm account hasn't generated
any payouts yet, so account_value = -(fees + insurance). The constraint becomes:
"The Deribit portfolio must cover the total cost if the challenge fails."

**For funded phase**: The account has generated payouts, so account_value could be
positive. The constraint is more relaxed.

## 3.7 Theta-Positive In Flat Market Constraint

If ETH stays flat (price doesn't move all week), the Deribit portfolio must profit:

```
Σ over all legs: leg.quantity × leg.direction × leg.theta × spot × 7 > 0
```

(Theta is in ETH/day from Deribit, × spot = USD/day, × 7 = weekly)

This ensures we collect income even when nothing happens.

## 3.8 Path-Level Account Survival Constraint

For each path p, for each day d, the prop firm account must survive:

```
# MDD check: cumulative loss must not exceed limit
cumulative_loss(p, d) ≤ mdd_limit

# DDD check: daily loss must not exceed limit
daily_loss(p, d) ≤ ddd_limit
```

If MDD is breached on any path → account is dead on that path → no future payouts.
The optimizer must account for this: on paths where the account dies,
prop_pnl = -(whatever was lost up to death), and no payout is possible.

---

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: MILP FORMULATION (THE CORE ENGINE)
# ═══════════════════════════════════════════════════════════════════════════════

## 4.1 Overview

The MILP finds the OPTIMAL combination of Deribit options to buy/sell.
"Optimal" means: maximizes expected profit while guaranteeing no net loss.

It might choose 1 option, or 2, or 10. It might choose an iron condor,
or a butterfly, or just a single put. The MILP decides — we don't
constrain the structure.

## 4.2 Decision Variables

```
For each available Deribit option instrument i (i = 1..N):
    x_buy[i]  ∈ {0, 1, 2, ..., MAX_QTY}    # contracts to BUY (we pay ask price)
    x_sell[i] ∈ {0, 1, 2, ..., MAX_QTY}     # contracts to SELL (we receive bid price)

For each prop firm account a (a = 1..A):
    dir[a] ∈ {0, 1}                          # 0 = long, 1 = short (binary)

MAX_QTY = 50 (configurable, start with 50)
```

**WHY separate x_buy and x_sell?** Because buy price ≠ sell price (bid/ask spread).
If we had a single variable x[i] that could be positive (buy) or negative (sell),
the cost function would be nonlinear (if x>0 use ask, if x<0 use bid).
Splitting into two variables keeps the MILP LINEAR.

**Net position**: `x_net[i] = x_buy[i] - x_sell[i]` (can be negative = net short)

## 4.3 Objective Function

```
MAXIMIZE:
    Σ over paths p (prob[p] × combined_pnl(p))

Where combined_pnl(p) =
    # Deribit P&L
    Σ_i (x_net[i] × option_value_at_friday[i, p]) - entry_cost
    # Plus prop firm P&L (path-dependent, see simulation)
    + prop_pnl(p)
```

Expanded entry cost:
```
entry_cost = Σ_i (x_buy[i] × ask[i] × spot) - Σ_i (x_sell[i] × bid[i] × spot)
```

## 4.4 Constraints

### C1: Zero Net Loss (End-State) — for each path p:
```
deribit_pnl(p) + prop_pnl(p) ≥ -tolerance

Where tolerance is a small buffer (e.g., $50) to prevent infeasibility from rounding.
```

**This is the HARDEST constraint.** It must hold for ALL paths, including adverse ones.

### C2: Account Survival — for each path p, each day d:
```
# This is NOT a MILP constraint directly — it's computed during path simulation.
# The path simulation determines prop_pnl(p) accounting for stop-outs and reopens.
# Paths where the account dies have prop_pnl = -(loss at death), no payout.
```

### C3: Theta Positive In Flat Market:
```
Σ_i (x_net[i] × theta[i] × spot) × 7 ≥ MIN_WEEKLY_THETA
Where MIN_WEEKLY_THETA = 50  # at least $50/week in flat market (configurable)
```

### C4: Margin Budget:
```
Σ_i margin_required[i] × (x_buy[i] + x_sell[i]) ≤ MARGIN_BUDGET
```
margin_required comes from Deribit API or approximation (mark_price × spot × 1.25).

### C5: Max Contracts Per Instrument:
```
x_buy[i] + x_sell[i] ≤ MAX_QTY  for all i
```

### C6: Greeks Limits (Safety):
```
|Σ_i x_net[i] × delta[i]| ≤ MAX_PORTFOLIO_DELTA  (e.g., 50)
|Σ_i x_net[i] × vega[i]|  ≤ MAX_PORTFOLIO_VEGA   (e.g., 100)
Σ_i x_net[i] × gamma[i]   ≥ -MAX_NEGATIVE_GAMMA  (e.g., -10)
```

### C7: Selling Constraint:
```
# You can only sell if you also have some protection (spread).
# For each sold option, there must be a bought option of the same type
# at a further-OTM strike. This prevents naked short risk.
# Implementation: for each sold call at strike K, require a bought call at strike K' > K.
# Same for puts: sold put at K requires bought put at K' < K.
```

**NOTE**: This constraint is important but complex. For MVP, implement it as:
"total sold contracts ≤ total bought contracts per option type (calls/puts separately)."
This is a simplification but prevents naked shorts.

## 4.5 Linearization Notes

The MILP must be LINEAR. Things that could make it nonlinear:

1. **option_value_at_friday** depends on Black-Scholes (nonlinear in x).
   SOLUTION: Precompute! For each instrument i and each path p, compute
   `V[i,p] = bs_price(friday_close[p], strike[i], T_remaining, iv[p], ...)`.
   This is a CONSTANT MATRIX, computed before the MILP runs.
   Then `deribit_pnl(p) = V[:,p] @ x_net - entry_cost` is LINEAR in x.

2. **prop_pnl depends on direction** (which is a binary variable).
   SOLUTION: Precompute prop_pnl for BOTH directions on each path.
   `prop_pnl_long[p]` and `prop_pnl_short[p]` are constants.
   Then: `prop_pnl[a,p] = dir[a] × prop_pnl_short[a,p] + (1-dir[a]) × prop_pnl_long[a,p]`
   This is linear because dir[a] is binary and the p&l values are constants.

3. **bid/ask split** is already handled by separate x_buy and x_sell variables.

## 4.6 Precomputation Steps (BEFORE MILP runs)

1. **Generate scenario paths** (Section 6.2): ~30-50 paths, each with 5 DayPoints
2. **For each path, simulate prop firm P&L** for both long and short directions,
   including DDD stop-outs, reopens, and MDD checks → get `prop_pnl_long[p]` and `prop_pnl_short[p]`
3. **For each instrument i and each path p**, compute Friday option value using BS:
   `V[i,p] = bs_price(friday_close[p], strike[i], T_remaining, iv[p], r, type[i])`
   This produces the "payoff matrix" — shape (N_instruments, N_paths)
4. **For each instrument i**, get: bid[i], ask[i], theta[i], delta[i], vega[i], gamma[i], margin[i]
5. **Now all inputs to the MILP are constants.** Only x_buy, x_sell, dir are variables.

## 4.7 MILP Solver

Use `cvxpy` with HiGHS backend (already in the existing codebase).

```python
import cvxpy as cp

# Variables
x_buy = cp.Variable(N, integer=True)
x_sell = cp.Variable(N, integer=True)
x_net = x_buy - x_sell

# Objective: maximize expected combined P&L
expected_pnl = sum(prob[p] * (V[:, p] @ x_net - entry_cost + prop_pnl[p]) for p in paths)
objective = cp.Maximize(expected_pnl)

# Constraints (list)
constraints = [
    x_buy >= 0, x_buy <= MAX_QTY,
    x_sell >= 0, x_sell <= MAX_QTY,
    # ... all constraints from 4.4
]

prob = cp.Problem(objective, constraints)
prob.solve(solver=cp.HIGHS)
```

---

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5: MODULE-BY-MODULE BUILD INSTRUCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

Build in this exact order. Each module must work independently before moving on.

## Module 1: `prop_firm.py` — Prop Firm Model

**What it does**: Defines all prop firm data structures and computes derived values.

**Contains**: `PropFirmRules`, `PhaseConfig`, `AccountState` (from Section 2.1)

**Functions to implement**:
```python
def create_account(rules: PropFirmRules, phase_index: int = 0) -> AccountState:
    """Create a new account state at the beginning of a phase."""

def advance_phase(account: AccountState) -> AccountState:
    """Move account to next phase (e.g., challenge → funded)."""

def compute_payout(account: AccountState) -> float:
    """Compute payout amount for current funded phase."""
```

**Test**: Create a 1-Step Pro account ($100K, 5% MDD, 3% DDD, 80% split, $700 fee).
Verify: payout = $4,000, safe_risk = $1,250, ddd_limit = $3,000.

**File size**: ~150 lines.

---

## Module 2: `forecast.py` — Forecast Parser + Scenario Generator

**What it does**: Takes a `WeeklyForecast` and generates `ScenarioPath` objects.

**Contains**: `DailyForecast`, `PriceRangeBucket`, `WeeklyForecast`, `DayPoint`, `ScenarioPath`

### Path Generation Algorithm

The user provides Friday price range probabilities (e.g., "$1800-1900: 15%").
We need to generate FULL WEEKLY PATHS (Mon-Fri with OHLC per day), not just Friday endpoints.

**Step 1: Generate Friday close prices**
For each PriceRangeBucket, use the midpoint as the representative Friday close.
Example: $1800-1900 bucket → Friday close = $1,850

**Step 2: Build daily paths from Monday to Friday**
For each Friday target, interpolate a path:
```
For each day d (0=Mon, 1=Tue, ..., 4=Fri):
    progress = (d + 1) / 5  # 0.2, 0.4, 0.6, 0.8, 1.0
    expected_close[d] = spot + (friday_close - spot) × progress

    # Add daily noise based on sentiment and volatility
    if daily_forecast[d].sentiment == "bullish":
        bias = +0.3 × daily_volatility  # slight upward bias
    elif daily_forecast[d].sentiment == "bearish":
        bias = -0.3 × daily_volatility
    else:
        bias = 0

    close[d] = expected_close[d] + bias

    # High and low for DDD checks
    high[d] = close[d] + 0.6 × daily_volatility
    low[d]  = close[d] - 0.6 × daily_volatility

    # Open = previous close (or spot for Monday)
    open[d] = close[d-1] if d > 0 else spot
```

**Step 3: Compute IV per day** using the IV trajectory model (Section 3.3)
```
For each day d:
    price_change_pct = (close[d] - spot) / spot
    iv[d] = iv_at_point(current_iv, price_change_pct)
```

**Step 4: Create adverse paths**
For the 20% "bad week" scenarios:
- Take the forecast direction, INVERT it
- Multiply volatility by 1.5
- Widen price ranges by 30%

Generate 3-5 adverse paths with total probability = 0.20
(Distribute: 0.08 for inverted direction, 0.06 for tail up, 0.06 for tail down)

**Step 5: Normalize probabilities**
All path probabilities must sum to 1.0.

**Target**: ~30-50 total paths (25-40 from forecast + 5-10 adverse)

**Functions to implement**:
```python
def parse_forecast(report_text: str) -> WeeklyForecast:
    """Parse the raw intelligence report text into structured forecast.
    NOTE: For MVP, the user will fill in WeeklyForecast manually.
    This function is a placeholder for future automation."""

def generate_paths(forecast: WeeklyForecast, current_iv: float, n_paths: int = 40) -> list[ScenarioPath]:
    """Generate scenario paths from forecast."""

def generate_adverse_paths(forecast: WeeklyForecast, current_iv: float, n_adverse: int = 5) -> list[ScenarioPath]:
    """Generate adverse (bad week) scenario paths."""
```

**Test**: Given a bearish forecast with spot=$2,000 and range $1,800-$2,100:
- Most paths should end below $2,000 on Friday
- Adverse paths should include a path ending above $2,200
- All probabilities sum to 1.0
- Every DayPoint has high > close > low

**File size**: ~300 lines.

---

## Module 3: `path_simulator.py` — Prop Firm Path Simulation

**What it does**: Takes a set of ScenarioPaths + AccountState + direction,
simulates day-by-day trading on the prop firm, including DDD stop-outs,
reopens, and MDD checks.

**This is the most complex module.** Build it carefully.

### Simulation Logic (for ONE path, ONE account, ONE direction):

```python
def simulate_path(
    path: ScenarioPath,
    account: AccountState,
    direction: str,  # "long" or "short"
    entry_price: float,  # price at which prop firm position is opened
) -> PathResult:
    """
    Simulate one week of prop firm trading on one path.

    Returns PathResult with:
        - final_pnl: total P&L at end of Friday
        - account_alive: True if MDD was not breached
        - daily_pnls: list of daily P&L values
        - stop_out_events: list of (day, price, loss) for each stop-out
        - reopen_events: list of (day, price, new_direction) for each reopen
    """
    position_entry = entry_price
    current_direction = direction
    cumulative_pnl = 0.0
    daily_pnls = []
    account_alive = True

    for day in path.days:
        daily_pnl = 0.0

        # Check for DDD breach INTRADAY
        if current_direction == "long":
            worst_intraday = (day.low_price - position_entry) × position_size_eth
        else:  # short
            worst_intraday = (position_entry - day.high_price) × position_size_eth

        if worst_intraday < -safe_risk_per_trade:
            # STOP OUT — loss is capped at safe_risk_per_trade
            daily_pnl += -safe_risk_per_trade - close_reopen_cost

            # Can we reopen? Check remaining DDD budget for today
            remaining_ddd = ddd_limit - abs(daily_pnl)
            if remaining_ddd >= safe_risk_per_trade:
                # REOPEN — direction is pre-determined by optimizer
                # For simulation: use same direction (optimizer will choose best)
                position_entry = day.close_price  # approximate reopen price
                # Second half of day P&L
                # (simplified: assume reopen at mid-day, end at close)
                # For MVP: second trade P&L = 0 (conservative)
            else:
                # Can't reopen — done for today
                position_entry = None  # no position
        else:
            # No stop-out — normal day
            if current_direction == "long":
                daily_pnl = (day.close_price - position_entry) × position_size_eth
            else:
                daily_pnl = (position_entry - day.close_price) × position_size_eth
            position_entry = day.close_price  # mark-to-market reentry

        cumulative_pnl += daily_pnl
        daily_pnls.append(daily_pnl)

        # Check MDD
        if cumulative_pnl < -mdd_limit:
            account_alive = False
            break  # account dead

    return PathResult(
        final_pnl=cumulative_pnl,
        account_alive=account_alive,
        daily_pnls=daily_pnls,
        ...
    )
```

**Functions to implement**:
```python
def simulate_path(path, account, direction, entry_price) -> PathResult:
    """Simulate one path for one account in one direction."""

def simulate_all_paths(paths, account, direction) -> list[PathResult]:
    """Simulate all paths for one account in one direction."""

def precompute_prop_pnl(paths, account) -> tuple[np.ndarray, np.ndarray]:
    """
    Returns (prop_pnl_long, prop_pnl_short) — two arrays of shape (n_paths,).
    prop_pnl_long[p] = final P&L if we go long on path p.
    prop_pnl_short[p] = final P&L if we go short on path p.
    These are CONSTANTS fed into the MILP.
    """
```

**Test**: Create a simple 5-day path where ETH drops 3% on day 2.
For a long position with 3% DDD, verify stop-out happens on day 2.
Verify cumulative P&L accounts for the stop-out loss.

**File size**: ~250 lines.

---

## Module 4: `bs_pricer.py` — Black-Scholes + IV Model

**What it does**: Pure math module. No API calls, no side effects.

**Functions to implement**:
```python
def bs_price(S, K, T, sigma, r, option_type) -> float:
    """Black-Scholes option price. T in YEARS."""

def bs_delta(S, K, T, sigma, r, option_type) -> float:
    """BS delta."""

def bs_theta(S, K, T, sigma, r, option_type) -> float:
    """BS theta (per day, in USD)."""

def iv_at_point(base_iv, price_change_pct) -> float:
    """IV estimate given a price move. See Section 3.3."""

def iv_at_strike(atm_iv, strike, spot) -> float:
    """IV adjusted for smile/skew. See Section 3.5."""

def build_valuation_matrix(
    instruments: list[OptionInstrument],
    paths: list[ScenarioPath],
    current_spot: float,
    current_iv: float,
    days_to_expiry: float,
) -> np.ndarray:
    """
    Build the precomputed option value matrix V[i, p].
    V[i, p] = BS value of instrument i if path p's Friday close happens.

    Shape: (n_instruments, n_paths)
    This is a CONSTANT matrix fed into the MILP.
    """
    for i, inst in enumerate(instruments):
        for p, path in enumerate(paths):
            friday = path.days[-1]
            T_remaining = (days_to_expiry - 5) / 365  # time left after Friday
            iv = iv_at_strike(friday.iv_atm, inst.strike, friday.close_price)
            V[i, p] = bs_price(friday.close_price, inst.strike, T_remaining, iv, r, inst.option_type)
    return V
```

**Test**: BS put at strike=2000, spot=2000, T=7/365, IV=0.65 → should be ~$80-120.
BS call at same params → should be ~$80-120 (ATM call ≈ ATM put for short-dated).
Verify put-call parity: Call - Put = S - K×exp(-rT)

**File size**: ~200 lines.

---

## Module 5: `milp_solver.py` — The MILP Engine

**What it does**: Takes all precomputed data and solves for optimal option portfolio.

**This is the core of the tool.** It uses cvxpy with HiGHS.

**Inputs** (all precomputed constants):
```python
@dataclass
class MILPInput:
    # Option data
    n_instruments: int
    bid_prices: np.ndarray        # shape (N,) in USD
    ask_prices: np.ndarray        # shape (N,) in USD
    theta_usd: np.ndarray         # shape (N,) in USD/day
    delta: np.ndarray             # shape (N,)
    vega: np.ndarray              # shape (N,)
    gamma: np.ndarray             # shape (N,)
    margin: np.ndarray            # shape (N,) in USD

    # Precomputed matrices
    V: np.ndarray                 # shape (N, P) — Friday option values per path
    prop_pnl_long: np.ndarray     # shape (P,) — prop firm P&L if long
    prop_pnl_short: np.ndarray    # shape (P,) — prop firm P&L if short
    path_probs: np.ndarray        # shape (P,) — probability of each path

    # Instrument metadata (for spread constraint)
    strikes: np.ndarray           # shape (N,)
    is_call: np.ndarray           # shape (N,) boolean
    is_put: np.ndarray            # shape (N,) boolean

    # Params
    spot: float
    margin_budget: float
    max_qty: int
    min_weekly_theta: float       # minimum theta income in flat market
    max_portfolio_delta: float
    max_portfolio_vega: float
    max_negative_gamma: float
    loss_tolerance: float         # small buffer for zero-loss constraint (e.g., 50)
```

**Functions to implement**:
```python
def solve_weekly(inp: MILPInput) -> MILPOutput:
    """
    Solve the weekly MILP optimization.

    Returns MILPOutput with:
        - x_buy: array of buy quantities
        - x_sell: array of sell quantities
        - direction: "long" or "short"
        - expected_profit: expected combined P&L
        - worst_case_pnl: minimum combined P&L across all paths
        - entry_cost: net cost of the options portfolio
        - status: solver status
    """
```

**CRITICAL implementation detail**: The direction variable (long vs short) is binary.
But prop_pnl depends on direction, which makes the combined P&L constraint
involve a product of binary × continuous → nonlinear!

**SOLUTION**: Solve the MILP TWICE — once assuming long, once assuming short.
Compare the two results and pick the better one. This avoids the binary
direction variable entirely and keeps the MILP fully linear.

```python
def solve_weekly(inp: MILPInput) -> MILPOutput:
    result_long = _solve_for_direction(inp, direction="long")
    result_short = _solve_for_direction(inp, direction="short")

    if result_long.expected_profit >= result_short.expected_profit:
        return result_long
    else:
        return result_short

def _solve_for_direction(inp: MILPInput, direction: str) -> MILPOutput:
    """Solve MILP for a fixed direction."""
    prop_pnl = inp.prop_pnl_long if direction == "long" else inp.prop_pnl_short

    # Variables
    x_buy = cp.Variable(inp.n_instruments, integer=True)
    x_sell = cp.Variable(inp.n_instruments, integer=True)
    x_net = x_buy - x_sell

    # Entry cost (linear)
    cost = inp.ask_prices @ x_buy - inp.bid_prices @ x_sell

    # Objective
    expected_pnl = sum(
        inp.path_probs[p] * (inp.V[:, p] @ x_net * inp.spot - cost + prop_pnl[p])
        for p in range(len(inp.path_probs))
    )
    objective = cp.Maximize(expected_pnl)

    # Constraints
    constraints = [
        x_buy >= 0,
        x_sell >= 0,
        x_buy <= inp.max_qty,
        x_sell <= inp.max_qty,
    ]

    # C1: Zero net loss per path
    for p in range(len(inp.path_probs)):
        deribit_pnl_p = inp.V[:, p] @ x_net * inp.spot - cost
        constraints.append(deribit_pnl_p + prop_pnl[p] >= -inp.loss_tolerance)

    # C3: Theta positive in flat market
    constraints.append(inp.theta_usd @ x_net * 7 >= inp.min_weekly_theta)

    # C4: Margin budget
    constraints.append(inp.margin @ (x_buy + x_sell) <= inp.margin_budget)

    # C5: Max contracts per instrument
    for i in range(inp.n_instruments):
        constraints.append(x_buy[i] + x_sell[i] <= inp.max_qty)

    # C6: Greeks limits
    constraints.append(cp.abs(inp.delta @ x_net) <= inp.max_portfolio_delta)
    constraints.append(cp.abs(inp.vega @ x_net) <= inp.max_portfolio_vega)
    constraints.append(inp.gamma @ x_net >= -inp.max_negative_gamma)

    # C7: No naked shorts (simplified: sold ≤ bought per type)
    call_mask = inp.is_call.astype(float)
    put_mask = inp.is_put.astype(float)
    constraints.append(call_mask @ x_sell <= call_mask @ x_buy)
    constraints.append(put_mask @ x_sell <= put_mask @ x_buy)

    # Solve
    problem = cp.Problem(objective, constraints)
    problem.solve(solver=cp.HIGHS, time_limit=300)

    # Extract results
    ...
```

**Test**: Create 3 synthetic instruments (ATM put, ATM call, OTM put) and 3 paths
(up 5%, flat, down 5%). Verify solver finds a solution. Verify zero-loss constraint
holds for all paths.

**File size**: ~400 lines.

---

## Module 6: `weekly_engine.py` — The Main Orchestrator

**What it does**: Ties everything together. This is what the user runs on Sunday.

**Workflow**:
```
1. User provides: WeeklyForecast (manually for MVP) + list of AccountState
2. Fetch live Deribit data (reuse optimizer_step1.py)
3. Generate scenario paths (forecast.py)
4. For each account:
   a. Simulate prop firm paths for long AND short (path_simulator.py)
   b. Build valuation matrix (bs_pricer.py)
   c. Run MILP (milp_solver.py) → get optimal options portfolio + direction
   d. Compute EV, worst-case, ROI metrics
5. Rank all accounts by ROI-per-day
6. Compute "do nothing" baseline (just hold existing positions, collect theta)
7. Output: formatted comparison table + recommendation
```

**Functions to implement**:
```python
async def run_weekly_decision(
    forecast: WeeklyForecast,
    accounts: list[AccountState],
    margin_budget: float,
    max_qty: int = 50,
) -> WeeklyDecision:
    """Main entry point. Runs the full weekly analysis."""

def format_decision(decision: WeeklyDecision) -> str:
    """Format the decision as a readable text table for the user."""

def format_execution_instructions(play: PlayRecommendation) -> str:
    """Format exact Deribit orders for the user to execute."""
```

**Output format** (what the user sees):
```
═══════════════════════════════════════════════════════════════
WEEKLY DECISION — 2026-04-06
ETH Spot: $2,066 | Forecast: BEARISH (68%) | ATR: $180/day
═══════════════════════════════════════════════════════════════

RANKED PLAYS:
┌────┬──────────┬──────────┬───────────┬───────────┬──────────┬─────────┐
│ #  │ Account  │ Dir      │ Ins. Cost │ Exp. Profit│ Worst    │ ROI/day │
├────┼──────────┼──────────┼───────────┼───────────┼──────────┼─────────┤
│ 1  │ UpScale  │ SHORT    │ $1,200    │ $3,400    │ $200     │ 0.40%   │
│ 2  │ NewAcct  │ SHORT    │ $800      │ $2,100    │ $50      │ 0.35%   │
│ 3  │ DO NOTHING│ -       │ $0        │ $300      │ -$50     │ 0.05%   │
└────┴──────────┴──────────┴───────────┴───────────┴──────────┴─────────┘

★ RECOMMENDED: Play #1 (UpScale SHORT)

EXECUTION INSTRUCTIONS:
  SELL 3x ETH-11APR26-2225-C @ 0.0120 ETH ($24.79)
  BUY  3x ETH-11APR26-2250-C @ 0.0085 ETH ($17.56)
  SELL 3x ETH-11APR26-1950-P @ 0.0200 ETH ($41.32)
  BUY  3x ETH-11APR26-1800-P @ 0.0050 ETH ($10.33)
  NET CREDIT: $114.66 (theta: $18.20/day)
```

**File size**: ~300 lines.

---

## Module 7: `config.py` — Configuration Presets

**What it does**: Stores known prop firm configurations so the user doesn't
have to type rules every time.

```python
# Pre-configured prop firm plans
UPSCALE_1STEP_PRO_100K = PropFirmRules(
    firm_name="UpScale",
    plan_name="1-Step Pro $100K",
    account_size=100_000,
    fee=700,
    phases=[
        PhaseConfig("Challenge", profit_target_pct=0.12, mdd_pct=0.05, ddd_pct=0.03, min_trading_days=3),
        PhaseConfig("Funded", profit_target_pct=0.05, mdd_pct=0.05, ddd_pct=0.03, min_trading_days=3, is_funded=True),
    ],
    payout_split=0.80,
    ddd_uses_equity=True,
    mdd_is_trailing=False,
)

# Add more presets as needed
```

**File size**: ~100 lines.

---

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6: INTEGRATION + TESTING
# ═══════════════════════════════════════════════════════════════════════════════

## 6.1 End-to-End Test Scenario

Create a test that runs the FULL pipeline with synthetic data:

```python
def test_full_pipeline():
    # 1. Create a forecast
    forecast = WeeklyForecast(
        eth_spot_at_report=2000,
        overall_direction="bearish",
        overall_confidence=0.68,
        friday_price_ranges=[
            PriceRangeBucket(1800, 1900, 0.15),
            PriceRangeBucket(1900, 2000, 0.40),
            PriceRangeBucket(2000, 2100, 0.30),
            PriceRangeBucket(2100, 2200, 0.15),
        ],
        weekly_atr_usd=180,
        daily_forecasts=[...],
    )

    # 2. Create an account
    account = create_account(UPSCALE_1STEP_PRO_100K)

    # 3. Generate paths
    paths = generate_paths(forecast, current_iv=0.65)

    # 4. Run decision engine
    decision = await run_weekly_decision(forecast, [account], margin_budget=10000)

    # 5. Verify
    assert decision.plays[0].worst_case_profit_usd >= -50  # near-zero-loss
    assert decision.plays[0].portfolio_theta_usd_day > 0   # theta positive
    assert len(decision.plays) >= 1
```

## 6.2 Critical Invariants To Test

These must ALWAYS hold. If any test fails, something is fundamentally wrong:

1. **All path probabilities sum to 1.0** (within 0.001 tolerance)
2. **Every DayPoint has high >= close >= low** (always)
3. **BS put-call parity holds**: Call - Put = S - K×exp(-rT) (within $0.01)
4. **Zero-loss constraint holds for EVERY path** in the MILP output
5. **Theta is positive in flat market** (Σ theta × quantity × 7 > 0)
6. **No naked shorts**: sold contracts ≤ bought contracts per type
7. **prop_pnl_long and prop_pnl_short have opposite signs** on trending paths
   (if ETH goes up, long profits, short loses — and vice versa)
8. **Margin used ≤ margin budget** (from MILP output)

## 6.3 What To Test With Real Deribit Data

After the synthetic tests pass, run against LIVE data:
```
python weekly_engine.py --live --forecast manual
```

This should:
1. Fetch real options from Deribit (using optimizer_step1.py)
2. Use a manually entered forecast
3. Run the full pipeline
4. Print the decision table

Verify:
- Instrument names are real Deribit names (e.g., "ETH-11APR26-2200-C")
- Prices are reasonable (not $0 or $99999)
- The solver finds a feasible solution (status = OPTIMAL)
- The recommended instruments actually exist on Deribit

---

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7: WHAT IS EXPLICITLY OUT OF SCOPE (DO NOT BUILD)
# ═══════════════════════════════════════════════════════════════════════════════

Do NOT build any of these in the MVP:

1. ❌ Automatic report parsing (user enters forecast manually)
2. ❌ Real Deribit order execution (advisory only — user executes manually)
3. ❌ Historical backtesting engine
4. ❌ Web UI or dashboard
5. ❌ Database or persistent storage
6. ❌ Real-time monitoring or alerts
7. ❌ Multi-day restructuring engine (one week at a time for MVP)
8. ❌ Automatic retry on failed challenges
9. ❌ BTC→ETH correlation translation (user provides ETH forecast directly)
10. ❌ Portfolio margin optimization (use simple margin approximation)

---

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8: FILE STRUCTURE (FINAL)
# ═══════════════════════════════════════════════════════════════════════════════

```
d:/Options_Trading/
├── optimizer_step1.py      # EXISTING — Deribit data fetch (REUSE, do not rewrite)
├── optimizer_step2.py      # EXISTING — Payoff engine (REFERENCE only)
├── optimizer_step3.py      # EXISTING — Base MILP (REFERENCE only)
├── optimizer_step4.py      # EXISTING — Extended MILP (REFERENCE for cvxpy patterns)
├── optimizer_step5.py      # EXISTING — Margin refinement (REUSE fetch_real_margins)
├── optimizer.py            # EXISTING — Old CLI (do not modify)
│
├── prop_firm.py            # NEW — Module 1: Prop firm model
├── forecast.py             # NEW — Module 2: Forecast + path generation
├── path_simulator.py       # NEW — Module 3: Prop firm path simulation
├── bs_pricer.py            # NEW — Module 4: Black-Scholes + IV model
├── milp_solver.py          # NEW — Module 5: Weekly MILP optimizer
├── weekly_engine.py        # NEW — Module 6: Main orchestrator
├── config.py               # NEW — Module 7: Prop firm presets
```

---

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9: DEPENDENCY GRAPH
# ═══════════════════════════════════════════════════════════════════════════════

```
config.py ──→ prop_firm.py
                  │
                  ▼
forecast.py ──→ path_simulator.py
                  │
                  ▼
optimizer_step1.py ──→ bs_pricer.py ──→ milp_solver.py ──→ weekly_engine.py
                                                              │
                                                              ▼
                                                         (USER OUTPUT)
```

Build order: config → prop_firm → forecast → path_simulator → bs_pricer → milp_solver → weekly_engine

Each module depends ONLY on modules built before it. No circular dependencies.

---

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 10: QUICK REFERENCE — CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════

```python
# Risk-free rate
RISK_FREE_RATE = 0.05

# IV Model
IV_BETA_DOWN = 2.0          # vol points per 1% price decline
IV_BETA_UP = 1.0            # vol points per 1% price rally
IV_GAMMA_DOWN = 20.0        # convexity on downside
IV_GAMMA_UP = 5.0           # convexity on upside
IV_MEDIAN = 0.65            # long-run median IV
IV_MR_SPEED = 0.14          # daily mean reversion speed
IV_FLOOR = 0.35             # minimum IV
IV_CEILING = 2.00           # maximum IV

# Skew (simple model)
SKEW_MONEYNESS_LINEAR = 0.1
SKEW_MONEYNESS_QUADRATIC = 0.3

# MILP defaults
DEFAULT_MAX_QTY = 50
DEFAULT_MARGIN_BUDGET = 10_000  # USD
DEFAULT_MIN_WEEKLY_THETA = 50   # USD
DEFAULT_MAX_PORTFOLIO_DELTA = 50
DEFAULT_MAX_PORTFOLIO_VEGA = 100
DEFAULT_MAX_NEGATIVE_GAMMA = 10
DEFAULT_LOSS_TOLERANCE = 50     # USD

# Path generation
DEFAULT_N_PATHS = 40
DEFAULT_N_ADVERSE = 5
ADVERSE_VOL_MULTIPLIER = 1.5
ADVERSE_RANGE_MULTIPLIER = 1.3
ADVERSE_TOTAL_PROB = 0.20

# Prop firm defaults
DEFAULT_SLIPPAGE_PCT = 0.0025
DEFAULT_CLOSE_REOPEN_COST = 0.0
DEFAULT_PAYOUT_DELAY_DAYS = 1
DEFAULT_MIN_TRADING_DAYS = 3

# Deribit API
DERIBIT_BASE_URL = "https://www.deribit.com/api/v2"
DERIBIT_TESTNET_URL = "https://test.deribit.com/api/v2"
```
