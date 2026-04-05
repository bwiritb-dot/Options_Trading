"""
ETH Options Optimizer — Dash Chart Module
==========================================
Auto-launches in the browser after the optimizer solves.

Three P&L lines (USD, Y-axis) vs ETH spot price (X-axis):
  Green  (#00CC96): P&L at current time — moves with the time slider
                    BS mid-price at remaining DTE = original_dte − slider_value
  Yellow (#FFD700): P&L at holding-period end date — static
                    BS mid-price at remaining DTE = original_dte − holding_days
  Blue   (#636EFA): P&L at final expiration — static
                    Pure intrinsic value (no time value)

Fees baked in:
  Entry : taker fee = 0.05% × spot × |qty| per leg,
          capped at 12.5% × mid_price × spot × |qty|
  Blue  : + delivery fee = 0.015% × spot × |qty| per leg
  All lines also deduct the net option premium (ask for longs, bid for shorts).

X-axis: spot × 0.5 → spot × 1.6, 200 points
Time slider: 0 → holding_days, step 0.25 days

Usage (from optimizer.py):
    from optimizer_chart import launch_chart
    launch_chart(result=final_result, pkg=final_pkg, params=params, spot_price=spot_price)
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np

from optimizer_step4 import (
    ExtendedMILPResult,
    ExtendedMILPParams,
    RISK_FREE_RATE,
    MIN_TIME_TO_EXPIRY,
)
from optimizer_step2 import PayoffPackage

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

N_SPOT_POINTS     = 200
SPOT_LOW_MULT     = 0.85
SPOT_HIGH_MULT    = 1.15
TAKER_FEE_RATE    = 0.0005    # 0.05% of spot × |qty|
TAKER_FEE_CAP     = 0.125     # capped at 12.5% × mid_premium × spot × |qty|
DELIVERY_FEE_RATE = 0.00015   # 0.015% of spot × |qty|  (blue line only)

# ─────────────────────────────────────────────────────────────────────────────
# VECTORIZED BLACK-SCHOLES
# ─────────────────────────────────────────────────────────────────────────────

def _norm_cdf_vec(x: np.ndarray) -> np.ndarray:
    """Vectorized standard normal CDF via math.erf."""
    return 0.5 * (1.0 + np.vectorize(math.erf)(x / math.sqrt(2.0)))


def _bs_price_vec(
    S: np.ndarray,
    K: float,
    T: float,
    sigma: float,
    r: float,
    option_type: str,
) -> np.ndarray:
    """
    Vectorized Black-Scholes price for an array of spot prices.

    Returns intrinsic value when T <= MIN_TIME_TO_EXPIRY (near-expiry edge case).
    Result is in USD per contract (1 contract = 1 ETH).

    Args:
        S:           Array of spot prices (USD).
        K:           Strike (USD).
        T:           Time to expiry in years.
        sigma:       Implied volatility as a decimal (e.g. 0.80 = 80%).
        r:           Risk-free rate (decimal, annual).
        option_type: "call" or "put".
    """
    if T <= MIN_TIME_TO_EXPIRY:
        if option_type == "call":
            return np.maximum(S - K, 0.0)
        return np.maximum(K - S, 0.0)

    sigma    = max(0.01, min(5.0, sigma))
    sqrt_T   = math.sqrt(T)
    discount = math.exp(-r * T)

    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    if option_type == "call":
        return S * _norm_cdf_vec(d1) - K * discount * _norm_cdf_vec(d2)
    return K * discount * _norm_cdf_vec(-d2) - S * _norm_cdf_vec(-d1)


# ─────────────────────────────────────────────────────────────────────────────
# COST / FEE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _entry_costs(
    pkg: PayoffPackage,
    qty: np.ndarray,
    entry_spot: float,
) -> tuple[float, float]:
    """
    Compute entry costs at position open.

    Returns:
        (premium_usd, taker_fees_usd)

        premium_usd:   Net option premium paid (ask for longs, bid for shorts).
                       Positive = net debit, negative = net credit.
        taker_fees_usd: 0.05% × spot × |qty| per leg,
                        capped at 12.5% × mid × spot × |qty|.
    """
    x_pos = np.maximum(qty, 0.0)
    x_neg = np.maximum(-qty, 0.0)
    premium_usd = float(
        (pkg.entry.ask_vec @ x_pos - pkg.entry.bid_vec @ x_neg) * entry_spot
    )

    mid_vec = (pkg.entry.ask_vec + pkg.entry.bid_vec) / 2.0
    abs_qty = np.abs(qty)
    fee_per = np.minimum(TAKER_FEE_RATE * entry_spot,
                         TAKER_FEE_CAP * mid_vec * entry_spot)
    taker_fees_usd = float(np.sum(fee_per * abs_qty))

    return premium_usd, taker_fees_usd


def _delivery_fees(qty: np.ndarray, entry_spot: float) -> float:
    """0.015% × spot × |qty| per leg (applied to blue line only)."""
    return float(np.sum(DELIVERY_FEE_RATE * entry_spot * np.abs(qty)))


# ─────────────────────────────────────────────────────────────────────────────
# P&L COMPUTATION
# ─────────────────────────────────────────────────────────────────────────────

def _pnl_bs(
    pkg: PayoffPackage,
    qty: np.ndarray,
    spot_range: np.ndarray,
    t_elapsed: float,
    entry_spot: float,
) -> np.ndarray:
    """
    BS P&L across spot_range at elapsed time t_elapsed.

    Portfolio value = Σᵢ qᵢ × BS_price(S, Kᵢ, remaining_dteᵢ/365, σᵢ)
    P&L = portfolio_value − net_premium − taker_fees
    """
    portfolio = np.zeros(len(spot_range))
    for i, inst in enumerate(pkg.instruments):
        qi = float(qty[i])
        if qi == 0.0:
            continue
        remaining_dte = max(inst.days_to_expiry - t_elapsed, 0.0)
        T = remaining_dte / 365.0
        sigma = inst.mark_iv / 100.0
        leg_vals = _bs_price_vec(
            spot_range, inst.strike, T, sigma, RISK_FREE_RATE, inst.option_type
        )
        portfolio += qi * leg_vals

    premium, taker_fees = _entry_costs(pkg, qty, entry_spot)
    return portfolio - premium - taker_fees


def _pnl_intrinsic(
    pkg: PayoffPackage,
    qty: np.ndarray,
    spot_range: np.ndarray,
    entry_spot: float,
) -> np.ndarray:
    """
    Intrinsic-only P&L at expiration across spot_range (blue line).

    P&L = Σᵢ qᵢ × intrinsicᵢ(S) − net_premium − taker_fees − delivery_fees
    """
    portfolio = np.zeros(len(spot_range))
    for i, inst in enumerate(pkg.instruments):
        qi = float(qty[i])
        if qi == 0.0:
            continue
        if inst.option_type == "call":
            leg_vals = np.maximum(spot_range - inst.strike, 0.0)
        else:
            leg_vals = np.maximum(inst.strike - spot_range, 0.0)
        portfolio += qi * leg_vals

    premium, taker_fees = _entry_costs(pkg, qty, entry_spot)
    deliv = _delivery_fees(qty, entry_spot)
    return portfolio - premium - taker_fees - deliv


# ─────────────────────────────────────────────────────────────────────────────
# BREAKEVEN DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def _breakevens(spot_range: np.ndarray, pnl: np.ndarray) -> list[float]:
    """Return up to 2 breakeven prices (zero-crossing of pnl)."""
    result: list[float] = []
    for j in range(len(pnl) - 1):
        y0, y1 = pnl[j], pnl[j + 1]
        if (y0 <= 0.0 < y1) or (y0 >= 0.0 > y1):
            x0, x1 = spot_range[j], spot_range[j + 1]
            be = x0 - y0 * (x1 - x0) / (y1 - y0)
            result.append(be)
            if len(result) == 2:
                break
    return result


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def launch_chart(
    result: ExtendedMILPResult,
    pkg: PayoffPackage,
    params: ExtendedMILPParams,
    spot_price: float,
) -> None:
    """
    Launch the Dash P&L chart in the browser.

    Blocks until the user presses Ctrl+C. Call after print_final_report().

    Args:
        result:     Solved ExtendedMILPResult (x, active_legs).
        pkg:        PayoffPackage (instruments, entry vectors).
        params:     ExtendedMILPParams (holding_days, spot_price).
        spot_price: ETH spot price used at entry.
    """
    import dash
    from dash import dcc, html, Input, Output
    import plotly.graph_objects as go

    if result.x is None or not result.active_legs:
        print("  [chart] No active positions — skipping.")
        return

    qty          = result.x.astype(float)
    holding_days = params.holding_days
    today        = datetime.now()

    spot_range = np.linspace(
        spot_price * SPOT_LOW_MULT,
        spot_price * SPOT_HIGH_MULT,
        N_SPOT_POINTS,
    )

    # ── Pre-compute static lines (yellow and blue never change) ───────────────
    yellow_pnl = _pnl_bs(pkg, qty, spot_range, holding_days, spot_price)
    blue_pnl   = _pnl_intrinsic(pkg, qty, spot_range, spot_price)

    # ── Slider marks ──────────────────────────────────────────────────────────
    label_step = max(1, holding_days // 7) if holding_days > 7 else 1
    marks = {float(d): f"D{d}" for d in range(0, holding_days + 1, label_step)}

    # ── Figure factory (called once per slider update) ────────────────────────
    def _day_label(t: float) -> str:
        d = today + timedelta(days=t)
        return f"Day {t:.2f} of {holding_days}  ({d.strftime('%Y-%m-%d')})"

    def _build_fig(t: float) -> go.Figure:
        green_pnl = _pnl_bs(pkg, qty, spot_range, t, spot_price)
        be_list   = _breakevens(spot_range, green_pnl)

        # ── Compute y-axis range from actual data ─────────────────────────────
        all_pnl = np.concatenate([green_pnl, yellow_pnl, blue_pnl])
        y_min   = float(np.nanmin(all_pnl))
        y_max   = float(np.nanmax(all_pnl))
        pad     = max((y_max - y_min) * 0.05, 1.0)
        y_lo    = y_min - pad
        y_hi    = y_max + pad

        fig = go.Figure()

        # ── Red zone below P&L = 0 ────────────────────────────────────────────
        fig.add_hrect(
            y0=y_lo, y1=0,
            fillcolor="rgba(200, 0, 0, 0.13)",
            line_width=0,
            layer="below",
        )

        # ── Blue: intrinsic at expiration (static) ────────────────────────────
        fig.add_trace(go.Scatter(
            x=spot_range, y=blue_pnl,
            mode="lines",
            name="Expiry — intrinsic",
            line=dict(color="#636EFA", width=2),
        ))

        # ── Yellow: BS at holding-period end (static) ─────────────────────────
        fig.add_trace(go.Scatter(
            x=spot_range, y=yellow_pnl,
            mode="lines",
            name=f"Day {holding_days} — end (BS)",
            line=dict(color="#FFD700", width=2),
        ))

        # ── Green: BS at current slider time (updates) ────────────────────────
        fig.add_trace(go.Scatter(
            x=spot_range, y=green_pnl,
            mode="lines",
            name=f"Day {t:.2f} — now (BS)",
            line=dict(color="#00CC96", width=2.5),
        ))

        # ── Zero reference line ───────────────────────────────────────────────
        fig.add_hline(y=0, line_color="rgba(255,255,255,0.22)", line_width=1)

        # ── Current spot (dotted vertical) ────────────────────────────────────
        fig.add_vline(
            x=spot_price,
            line_dash="dot",
            line_color="rgba(255,255,255,0.55)",
            line_width=1.5,
            annotation_text=f"Spot  ${spot_price:,.0f}",
            annotation_position="top left",
            annotation_font_color="rgba(255,255,255,0.70)",
            annotation_font_size=11,
        )

        # ── Breakeven dashed verticals (up to 2) ──────────────────────────────
        for be in be_list:
            pct = (be - spot_price) / spot_price * 100.0
            pos = "top right" if pct >= 0 else "top left"
            fig.add_vline(
                x=be,
                line_dash="dash",
                line_color="#FF6B6B",
                line_width=1.5,
                annotation_text=f"BE  ${be:,.0f}  ({pct:+.1f}%)",
                annotation_position=pos,
                annotation_font_color="#FF6B6B",
                annotation_font_size=11,
            )

        # ── Layout ────────────────────────────────────────────────────────────
        fig.update_layout(
            paper_bgcolor="#1f2630",
            plot_bgcolor="#1a1e2a",
            font=dict(color="white", family="sans-serif"),
            title=dict(
                text="ETH Options Portfolio — P&L",
                font=dict(size=17),
                x=0.5,
            ),
            xaxis=dict(
                title="ETH Spot Price (USD)",
                gridcolor="rgba(255,255,255,0.08)",
                tickformat="$,.0f",
                tickprefix="",
            ),
            yaxis=dict(
                title="P&L (USD)",
                gridcolor="rgba(255,255,255,0.08)",
                tickformat="$,.0f",
                zeroline=False,
                range=[y_lo, y_hi],
            ),
            legend=dict(
                x=0.01, y=0.99,
                bgcolor="rgba(0,0,0,0.35)",
                bordercolor="rgba(255,255,255,0.18)",
                borderwidth=1,
            ),
            hovermode="x unified",
            height=540,
            margin=dict(l=75, r=40, t=58, b=40),
        )
        return fig

    # ── Dash app ──────────────────────────────────────────────────────────────
    app = dash.Dash(__name__, title="ETH Options P&L", update_title=None)

    app.layout = html.Div(
        style={
            "backgroundColor": "#1f2630",
            "minHeight": "100vh",
            "padding": "20px",
            "fontFamily": "sans-serif",
        },
        children=[
            dcc.Graph(id="pnl-chart", figure=_build_fig(0.0)),
            html.Div(
                style={"padding": "8px 44px 28px"},
                children=[
                    html.Div(
                        id="slider-label",
                        children=_day_label(0.0),
                        style={
                            "color": "#9BAAB5",
                            "marginBottom": "10px",
                            "fontSize": "13px",
                            "letterSpacing": "0.02em",
                        },
                    ),
                    dcc.Slider(
                        id="time-slider",
                        min=0,
                        max=holding_days,
                        step=0.25,
                        value=0.0,
                        marks=marks,
                        tooltip={"placement": "bottom", "always_visible": False},
                    ),
                ],
            ),
        ],
    )

    @app.callback(
        Output("pnl-chart", "figure"),
        Output("slider-label", "children"),
        Input("time-slider", "value"),
    )
    def _update(t):
        t = float(t or 0.0)
        return _build_fig(t), _day_label(t)

    print("\n  P&L chart → http://127.0.0.1:8050/")
    print("  Press Ctrl+C to stop.\n")
    import webbrowser
    webbrowser.open("http://127.0.0.1:8050/")
    app.run(debug=False)
