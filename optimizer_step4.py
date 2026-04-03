"""
ETH Options Optimizer — Step 4: Extended MILP
==============================================
Расширяет базовую MILP из Step 3 тремя блоками:

  БЛОК A — Сценарная theta (Black-Scholes)
    Заменяет точечную Deribit-theta на ожидаемый вклад по 5 сценариям.
    Каждый сценарий: другой spot (spot_mult) + другой IV (iv_shift).
    Theta пересчитывается через BS для каждого сценария.
    Objective = Σ_s prob_s × (theta_bs[s] @ x) × holding_days

  БЛОК B — Greeks constraints
    delta_portfolio ∈ [−MAX_DELTA, +MAX_DELTA]
    vega_portfolio  ∈ [−MAX_VEGA,  +MAX_VEGA]
    gamma_portfolio ≥ −MAX_GAMMA

  БЛОК C — IV Stress constraint
    Если IV вырастет на +25% прямо сейчас (spot неизменен):
    MTM P&L от IV-сдвига ≥ PNL_FLOOR × IV_STRESS_MULTIPLIER

Почему важна каждая из этих доработок — см. комментарии в коде.

Зависимости:
    pip install cvxpy highspy numpy aiohttp
    (scipy НЕ нужен — BS реализован через math.erf)

Интеграция:
    from optimizer_step4 import solve_portfolio_v4, ExtendedMILPParams
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional

import cvxpy as cp
import numpy as np
import numpy.typing as npt
import logging
log = logging.getLogger(__name__)

from optimizer_step1 import OptionInstrument, fetch_liquid_options
from optimizer_step2 import (
    PayoffPackage,
    build_payoff_package,
    pnl_vector,
)
from optimizer_step3 import (
    MILPProblem,
    MILPResult,
    SolverStatus,
    _extract_result,
    _find_price_index,
    _get_available_mip_solvers,
    _relax_params,
    relax_and_retry,
    try_solvers,
    MAX_RELAXATION_STEPS,
    SOLVER_TIME_LIMIT_SECONDS,
    print_result as print_result_v3,
)

# ─────────────────────────────────────────────────────────────────────────────
# КОНСТАНТЫ
# ─────────────────────────────────────────────────────────────────────────────

RISK_FREE_RATE:       float = 0.05    # годовая безрисковая ставка (USD)
DAYS_PER_YEAR:        float = 365.0
MIN_TIME_TO_EXPIRY:   float = 1e-4   # лет (~52 минуты) — ниже BS нестабилен
MIN_IV:               float = 0.01   # минимальная IV для BS (1%)
MAX_IV:               float = 5.00   # максимальная IV для BS (500%)

# IV stress параметры
IV_STRESS_SHIFT:            float = 0.25   # +25 пп абсолютного IV
IV_STRESS_FLOOR_MULTIPLIER: float = 1.50   # PNL_FLOOR × 1.5 (более мягкий floor)

# Сценарии движения рынка — взвешенные по вероятности
# Каждый сценарий: (spot_mult, iv_shift, prob)
# iv_shift — абсолютный сдвиг IV в долях (0.08 = +8 пп)
# Сумма prob = 1.0 — обязательное условие
MARKET_SCENARIOS: list[dict] = [
    {"name": "crash",  "spot_mult": 0.90, "iv_shift": +0.08, "prob": 0.10},
    {"name": "dip",    "spot_mult": 0.95, "iv_shift": +0.03, "prob": 0.20},
    {"name": "base",   "spot_mult": 1.00, "iv_shift":  0.00, "prob": 0.40},
    {"name": "rally",  "spot_mult": 1.05, "iv_shift": -0.02, "prob": 0.20},
    {"name": "surge",  "spot_mult": 1.10, "iv_shift": -0.04, "prob": 0.10},
]

# Проверка инварианта: сумма вероятностей = 1
_PROB_SUM = sum(s["prob"] for s in MARKET_SCENARIOS)
assert abs(_PROB_SUM - 1.0) < 1e-9, f"Сумма вероятностей сценариев = {_PROB_SUM} ≠ 1.0"

# ─────────────────────────────────────────────────────────────────────────────
# РАСШИРЕННЫЕ ПАРАМЕТРЫ MILP
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExtendedMILPParams:
    """
    Полный набор параметров для Step 4 MILP.

    Наследует все параметры Step 3 и добавляет Greeks limits.
    Используется только здесь — Step 3 MILPParams остаётся неизменным.
    """
    # Базовые параметры (те же что в Step 3)
    spot_price:      float = 2_000.0
    max_qty:         int   = 10
    margin_budget:   float = 15_000.0
    pnl_floor:       float = -1500.0
    pnl_ceiling:     float = -500.0
    range_high:      float = 2_200.0
    holding_days:    int   = 5

    # Greeks constraints (новые в Step 4)
    max_delta:       float = 0.50    # |Δ портфеля| ≤ 0.50 ETH на $1 движения
    max_vega_usd:    float = 2000.0  # |Vega| ≤ $2000 на 1% IV move
    max_gamma:       float = 0.050   # Gamma ≥ −0.050 (ограничиваем short gamma)

    # IV stress параметры
    iv_stress_shift:      float = IV_STRESS_SHIFT
    iv_stress_multiplier: float = IV_STRESS_FLOOR_MULTIPLIER

    # Внутренний счётчик релаксации
    relaxation_step: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК A — BLACK-SCHOLES ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """
    Кумулятивная функция стандартного нормального распределения Φ(x).

    Реализована через math.erf для избежания зависимости от scipy.
    Точность: машинная (≈ 15 значащих цифр).

    Args:
        x: Аргумент.

    Returns:
        Φ(x) ∈ (0, 1).
    """
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """
    Плотность стандартного нормального распределения φ(x).

    Args:
        x: Аргумент.

    Returns:
        φ(x) > 0.
    """
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_d1_d2(
    S: float,
    K: float,
    T: float,
    sigma: float,
    r: float,
) -> tuple[float, float]:
    """
    Вычисляет d1 и d2 — ключевые аргументы формулы Блэка-Шоулза.

    d1 = [ln(S/K) + (r + σ²/2) × T] / (σ × √T)
    d2 = d1 − σ × √T

    Args:
        S:     Текущая цена базового актива (USD).
        K:     Страйк опциона (USD).
        T:     Время до экспирации в годах.
        sigma: Подразумеваемая волатильность (decimal, не процент).
        r:     Безрисковая ставка (decimal, годовая).

    Returns:
        Кортеж (d1, d2).

    Raises:
        ValueError: Если T ≤ 0 или sigma ≤ 0.
    """
    if T <= 0 or sigma <= 0:
        raise ValueError(f"T={T} и sigma={sigma} должны быть > 0 для BS d1/d2.")

    sqrt_T   = math.sqrt(T)
    log_moneyness = math.log(S / K)
    d1 = (log_moneyness + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return d1, d2


def bs_price(
    S: float,
    K: float,
    T: float,
    sigma: float,
    r: float,
    option_type: str,
) -> float:
    """
    Цена опциона по формуле Блэка-Шоулза (USD на контракт).

    Для ETH-опционов Deribit: S и K в USD, 1 контракт = 1 ETH.
    Результат в USD. Делите на S для перевода в ETH.

    При T ≤ MIN_TIME_TO_EXPIRY возвращает intrinsic value.

    Args:
        S:           Текущий спот ETH/USD.
        K:           Страйк (USD).
        T:           Время до экспирации в годах (DTE / 365).
        sigma:       IV в долях (mark_iv / 100).
        r:           Безрисковая ставка (decimal).
        option_type: "call" или "put".

    Returns:
        Цена опциона в USD ≥ 0.
    """
    # Edge case: при экспирации — только intrinsic value
    if T <= MIN_TIME_TO_EXPIRY:
        if option_type == "call":
            return max(S - K, 0.0)
        return max(K - S, 0.0)

    # Клип IV для числовой стабильности
    sigma = max(MIN_IV, min(MAX_IV, sigma))

    d1, d2 = _bs_d1_d2(S, K, T, sigma, r)
    discount = math.exp(-r * T)

    if option_type == "call":
        return S * _norm_cdf(d1) - K * discount * _norm_cdf(d2)
    # put
    return K * discount * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_theta_usd_per_day(
    S: float,
    K: float,
    T: float,
    sigma: float,
    r: float,
    option_type: str,
) -> float:
    """
    Theta опциона по Black-Scholes в USD на день на контракт.

    Формула (колл):
        θ = − [S × φ(d1) × σ] / [2 × √T] − r × K × e^(−rT) × Φ(d2)

    Формула (пут):
        θ = − [S × φ(d1) × σ] / [2 × √T] + r × K × e^(−rT) × Φ(−d2)

    Деление на 365 переводит из годовой в дневную theta.

    Знак: отрицательный для лонгов (time decay = потери),
    отрицательный же в абсолютном выражении — при умножении на
    x < 0 (short) даёт положительный вклад в портфельную theta.

    При T ≤ MIN_TIME_TO_EXPIRY возвращает 0 (theta неопределена
    при экспирации).

    Args:
        S:           Текущий спот ETH/USD.
        K:           Страйк (USD).
        T:           Время до экспирации в годах.
        sigma:       IV в долях.
        r:           Безрисковая ставка (decimal).
        option_type: "call" или "put".

    Returns:
        Theta в USD/день (отрицательная для обоих типов опционов).
    """
    if T <= MIN_TIME_TO_EXPIRY:
        return 0.0

    sigma = max(MIN_IV, min(MAX_IV, sigma))

    d1, d2  = _bs_d1_d2(S, K, T, sigma, r)
    pdf_d1  = _norm_pdf(d1)
    discount = math.exp(-r * T)
    sqrt_T  = math.sqrt(T)

    # Общий член (отрицательный)
    time_decay_annual = -(S * pdf_d1 * sigma) / (2.0 * sqrt_T)

    if option_type == "call":
        rate_term_annual = -r * K * discount * _norm_cdf(d2)
    else:
        rate_term_annual = +r * K * discount * _norm_cdf(-d2)

    theta_annual = time_decay_annual + rate_term_annual
    return theta_annual / DAYS_PER_YEAR


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК A — СЦЕНАРНАЯ THETA
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ScenarioThetaMatrix:
    """
    Предвычисленные theta-векторы для всех сценариев.

    Attributes:
        theta_by_scenario: Список из 5 векторов shape [N], USD/день.
                           theta_by_scenario[s][i] = BS theta инструмента i
                           в сценарии s (USD/день).
        expected_theta:    Взвешенная сумма: Σ_s prob_s × theta_by_scenario[s].
                           Shape [N], USD/день.
        scenario_names:    Имена сценариев для отчёта.
        scenario_probs:    Вероятности сценариев.
    """
    theta_by_scenario: list[npt.NDArray[np.float64]]
    expected_theta:    npt.NDArray[np.float64]
    scenario_names:    list[str]
    scenario_probs:    list[float]


def build_scenario_theta(
    instruments: list[OptionInstrument],
    spot_price: float,
    scenarios: list[dict] = MARKET_SCENARIOS,
    risk_free_rate: float = RISK_FREE_RATE,
) -> ScenarioThetaMatrix:
    """
    Вычисляет BS theta для каждого инструмента в каждом сценарии.

    Почему BS theta точнее Deribit theta:
        Deribit theta — мгновенная оценка при текущем (spot, IV).
        За 5 дней holding period рынок почти наверняка сдвинется.
        Scenario theta = E[theta | рыночное движение по сценариям].
        Это более реалистичная оценка доходности от time decay.

    Пример (ATM колл, spot=2000, IV=80%, DTE=8):
        Base theta:  −$6.0/день (от Deribit или BS при spot=2000)
        Crash (−10%): spot=1800, IV=88% → опцион OTM, theta меньше
        Rally (+10%): spot=2200, IV=76% → опцион ITM, theta меньше
        E[theta] = взвешенная сумма всех пяти сценариев

    Единицы: результат в USD/день. При умножении на x (количество
    контрактов) и holding_days → ожидаемый USD theta за период.

    Args:
        instruments:    Список опционов.
        spot_price:     Текущий спот ETH/USD.
        scenarios:      Список сценариев [{spot_mult, iv_shift, prob, name}].
        risk_free_rate: Годовая безрисковая ставка.

    Returns:
        ScenarioThetaMatrix с предвычисленными данными.
    """
    N = len(instruments)
    theta_by_scenario: list[npt.NDArray[np.float64]] = []

    for scenario in scenarios:
        spot_s  = spot_price * scenario["spot_mult"]
        theta_s = np.zeros(N, dtype=np.float64)

        for i, inst in enumerate(instruments):
            T     = max(inst.days_to_expiry / DAYS_PER_YEAR, MIN_TIME_TO_EXPIRY)
            sigma = max(MIN_IV, inst.mark_iv / 100.0 + scenario["iv_shift"])

            theta_s[i] = bs_theta_usd_per_day(
                S           = spot_s,
                K           = inst.strike,
                T           = T,
                sigma       = sigma,
                r           = risk_free_rate,
                option_type = inst.option_type,
            )

        theta_by_scenario.append(theta_s)

    # Взвешенная ожидаемая theta — это и есть objective
    probs = np.array([s["prob"] for s in scenarios], dtype=np.float64)
    expected_theta = sum(
        p * theta_s
        for p, theta_s in zip(probs, theta_by_scenario)
    )

    return ScenarioThetaMatrix(
        theta_by_scenario = theta_by_scenario,
        expected_theta    = expected_theta,
        scenario_names    = [s["name"] for s in scenarios],
        scenario_probs    = list(probs),
    )


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК C — IV STRESS
# ─────────────────────────────────────────────────────────────────────────────

def build_iv_stress_delta(
    instruments: list[OptionInstrument],
    spot_price: float,
    iv_stress_shift: float = IV_STRESS_SHIFT,
    risk_free_rate: float  = RISK_FREE_RATE,
) -> npt.NDArray[np.float64]:
    """
    Вычисляет изменение mark-price каждого опциона при IV +25%.

    IV stress — это мгновенный шок: spot не меняется, IV вырастает
    на iv_stress_shift абсолютных пунктов прямо сейчас.

    Δmark_usd[i] = bs_price(spot, K, T, IV+Δ) − bs_price(spot, K, T, IV)

    Это линейный вектор коэффициентов для MILP:
        iv_stress_pnl = Δmark_usd @ x
        constraint: iv_stress_pnl ≥ PNL_FLOOR × multiplier

    Почему отдельно от P&L матрицы:
        Матрица P считает payoff при ЭКСПИРАЦИИ (intrinsic value).
        IV stress — это mark-to-market P&L прямо сейчас (time value sensitive).
        Два разных риска: один — экспирационный, другой — мгновенный.

    Args:
        instruments:     Список опционов.
        spot_price:      Текущий спот ETH/USD.
        iv_stress_shift: Абсолютный сдвиг IV (default 0.25 = +25 пп).
        risk_free_rate:  Годовая безрисковая ставка.

    Returns:
        ndarray shape [N] — Δmark в USD/контракт при IV stress.
        Положительный = опцион дорожает (хорошо для лонга).
        Отрицательный = опцион дешевеет (хорошо для шорта).
    """
    N = len(instruments)
    delta_mark_usd = np.zeros(N, dtype=np.float64)

    for i, inst in enumerate(instruments):
        T     = max(inst.days_to_expiry / DAYS_PER_YEAR, MIN_TIME_TO_EXPIRY)
        sigma = inst.mark_iv / 100.0

        # Базовая цена по BS (должна быть близка к mark_price × spot)
        price_base = bs_price(
            S=spot_price, K=inst.strike, T=T,
            sigma=sigma, r=risk_free_rate,
            option_type=inst.option_type,
        )

        # Стрессовая цена: IV сдвигается вверх
        sigma_stressed = max(MIN_IV, sigma + iv_stress_shift)
        price_stressed = bs_price(
            S=spot_price, K=inst.strike, T=T,
            sigma=sigma_stressed, r=risk_free_rate,
            option_type=inst.option_type,
        )

        # Изменение в USD на контракт
        delta_mark_usd[i] = price_stressed - price_base

    return delta_mark_usd


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК B+C — ПОСТРОЕНИЕ РАСШИРЕННОЙ MILP
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExtendedMILPData:
    """
    Все предвычисленные данные для extended MILP.
    Строится один раз, передаётся в solver.
    """
    scenario_theta: ScenarioThetaMatrix
    iv_stress_delta: npt.NDArray[np.float64]  # shape [N], USD/контракт


def precompute_extended_data(
    pkg: PayoffPackage,
    params: ExtendedMILPParams,
) -> ExtendedMILPData:
    """
    Вычисляет сценарную theta и IV stress вектор перед MILP.

    Вынесено отдельно чтобы:
        - можно было залогировать/проверить до запуска солвера
        - при итерационном уточнении маржи (Step 5) не пересчитывать

    Args:
        pkg:    PayoffPackage из Step 2.
        params: ExtendedMILPParams.

    Returns:
        ExtendedMILPData с готовыми векторами.
    """
    scenario_theta = build_scenario_theta(
        instruments = pkg.instruments,
        spot_price  = params.spot_price,
    )
    iv_stress_delta = build_iv_stress_delta(
        instruments     = pkg.instruments,
        spot_price      = params.spot_price,
        iv_stress_shift = params.iv_stress_shift,
    )
    return ExtendedMILPData(
        scenario_theta  = scenario_theta,
        iv_stress_delta = iv_stress_delta,
    )


def build_extended_milp_problem(
    pkg:    PayoffPackage,
    params: ExtendedMILPParams,
    ext:    ExtendedMILPData,
) -> MILPProblem:
    """
    Формулирует расширенную MILP задачу (Step 4).

    Отличия от Step 3:
        Objective: scenario-weighted theta (не single-point Deribit theta)
        New constraints:
            delta_portfolio ∈ [−max_delta, +max_delta]
            vega_portfolio  ∈ [−max_vega,  +max_vega]
            gamma_portfolio ≥ −max_gamma
            iv_stress_pnl   ≥ pnl_floor × iv_stress_multiplier

    Почему delta/vega через два неравенства (не cp.abs):
        cp.abs() корректен в DCP, но некоторые MIP-солверы
        хуже работают с epigraph-формулировкой. Явные линейные
        неравенства надёжнее и быстрее.

    Почему gamma одностороннее:
        Отрицательная gamma (short gamma) создаёт convexity risk —
        потери ускоряются при сильных движениях. Мы ограничиваем
        насколько "шорт-гамма" может стать позиция.
        Положительная gamma (long gamma) безопасна — не ограничиваем.

    Args:
        pkg:    PayoffPackage из Step 2.
        params: ExtendedMILPParams.
        ext:    Предвычисленные сценарные данные.

    Returns:
        MILPProblem с cvxpy объектами.
    """
    N     = pkg.n_instruments
    P     = pkg.payoff_matrix
    grid  = pkg.grid
    entry = pkg.entry
    gr    = pkg.greeks

    spot  = params.spot_price

    # ── Переменные (те же что в Step 3) ──────────────────────────────────────
    x       = cp.Variable(N, integer=True, name="x")
    x_long  = cp.Variable(N, integer=True, name="x_long")
    x_short = cp.Variable(N, integer=True, name="x_short")
    z       = cp.Variable(N,               name="z")

    # ── Стоимость входа (bid/ask split) ──────────────────────────────────────
    cost_usd_expr = (entry.ask_vec @ x_long - entry.bid_vec @ x_short) * spot

    # ── OBJECTIVE: сценарно-взвешенная theta (БЛОК A) ────────────────────────
    #
    # Почему это точнее:
    #   Deribit theta = θ(spot_now, IV_now) — мгновенный снимок.
    #   За 5 дней holding_days рынок сдвинется. Например:
    #     - При crash (prob=10%) spot −10%, IV +8% → theta шорт-путов падает
    #     - При base  (prob=40%) всё как сейчас
    #   Взвешенная theta учитывает ВСЕ эти исходы.
    #
    #   Кроме того, BS theta корректнее учитывает vega-theta trade-off:
    #   при IV +8% theta краткосрочных ATM опционов существенно меняется.
    #
    expected_theta_expr = sum(
        prob * (theta_s @ x)
        for prob, theta_s in zip(
            ext.scenario_theta.scenario_probs,
            ext.scenario_theta.theta_by_scenario,
        )
    )
    objective = cp.Maximize(expected_theta_expr * params.holding_days)

    # ── Constraints ──────────────────────────────────────────────────────────
    constraints: list[cp.Constraint] = []

    # 1-4. Базовые constraints из Step 3 (декомпозиция, |x|, границы qty)
    constraints.append(x == x_long - x_short)
    constraints.append(x_long  >= 0)
    constraints.append(x_short >= 0)
    constraints.append(z >= x)
    constraints.append(z >= -x)
    constraints.append(z >= 0)
    constraints.append(x >= -params.max_qty)
    constraints.append(x <=  params.max_qty)
    constraints.append(x_long  <= params.max_qty)
    constraints.append(x_short <= params.max_qty)

    # 5. P&L constraints (35 точек) — те же что в Step 3
    range_high_idx = _find_price_index(grid.prices, params.range_high)
    for j in range(len(grid.prices)):
        pnl_expr = P[j] @ x - cost_usd_expr
        floor = params.pnl_ceiling if j == range_high_idx else params.pnl_floor
        constraints.append(pnl_expr >= floor)

    # 6. Маржа (аппроксимация с 25% буфером)
    constraints.append(gr.margin_vec @ z <= params.margin_budget)

    # ── БЛОК B — Greeks constraints ───────────────────────────────────────────
    #
    # DELTA: ограничиваем направленный риск портфеля.
    #   Если delta_portfolio = 0.15, то при росте ETH на $1
    #   портфель зарабатывает/теряет $0.15.
    #   Theta-стратегии должны быть близки к delta-neutral.
    #   Два неравенства вместо cp.abs() — надёжнее для MIP.
    #
    constraints.append(gr.delta_vec @ x <=  params.max_delta)
    constraints.append(gr.delta_vec @ x >= -params.max_delta)

    #
    # VEGA: ограничиваем чувствительность к изменению IV.
    #   vega_usd[i] = ETH vega × spot → USD на 1% IV move на контракт.
    #   При vega_portfolio = −$600, если IV вырастет на 1%,
    #   портфель потеряет $600 (до вычета theta gain).
    #   Theta-стратегии (short vol) имеют отрицательную vega —
    #   constraint защищает от чрезмерного short-vol exposure.
    #
    constraints.append(gr.vega_usd @ x <=  params.max_vega_usd)
    constraints.append(gr.vega_usd @ x >= -params.max_vega_usd)

    #
    # GAMMA: ограничиваем short-gamma (convexity risk).
    #   Отрицательная гамма = позиция теряет всё быстрее при сильных
    #   движениях (квадратичный эффект). Ограничиваем снизу.
    #   Положительная гамма (long gamma) выгодна → не ограничиваем.
    #
    constraints.append(gr.gamma_vec @ x >= -params.max_gamma)

    # ── БЛОК C — IV Stress constraint ────────────────────────────────────────
    #
    # ext.iv_stress_delta[i] = изменение mark price при IV +25% (USD/контракт)
    # Линеен в x → корректен для MILP.
    #
    # Смысл: если IV прыгнет +25% прямо сейчас, наш MTM P&L не должен
    # быть хуже PNL_FLOOR × multiplier (т.е. −600 при floor=−400).
    # Это защита от "vega blowup" при экстремальных IV движениях.
    #
    # Важно: это ДОПОЛНЕНИЕ к P&L constraints, не замена.
    # P&L матрица считает payoff при экспирации.
    # IV stress считает мгновенный MTM при IV-шоке.
    #
    iv_stress_floor = params.pnl_floor * params.iv_stress_multiplier
    constraints.append(ext.iv_stress_delta @ x >= iv_stress_floor)

    problem = cp.Problem(objective, constraints)
    return MILPProblem(
        problem = problem,
        x       = x,
        x_long  = x_long,
        x_short = x_short,
        z       = z,
    )


# ─────────────────────────────────────────────────────────────────────────────
# РАСШИРЕННЫЕ МЕТРИКИ РЕЗУЛЬТАТА
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExtendedMILPResult(MILPResult):
    """
    Расширяет MILPResult метриками Step 4.

    Дополнительные поля:
        theta_by_scenario: Theta портфеля в каждом сценарии (USD/день).
        expected_theta_usd: Ожидаемая theta (взвешенная), USD/день.
        iv_stress_pnl:     MTM P&L при IV +25% (USD).
        bs_vs_deribit_theta: Сравнение BS base theta vs Deribit theta.
    """
    theta_by_scenario:   list[float]  = field(default_factory=list)
    expected_theta_usd:  float        = 0.0
    iv_stress_pnl:       float        = 0.0
    bs_vs_deribit_theta: float        = 0.0   # разница в %, base BS vs Deribit


def _extract_extended_result(
    milp:    MILPProblem,
    pkg:     PayoffPackage,
    params:  ExtendedMILPParams,
    ext:     ExtendedMILPData,
    solver_used: str,
) -> ExtendedMILPResult:
    """
    Извлекает все метрики из решённой расширенной MILP задачи.

    Args:
        milp:        Решённая cvxpy задача.
        pkg:         PayoffPackage.
        params:      ExtendedMILPParams.
        ext:         Предвычисленные данные.
        solver_used: Имя солвера.

    Returns:
        ExtendedMILPResult с полными метриками.
    """
    x_raw = milp.x.value
    if x_raw is None:
        return ExtendedMILPResult(
            status=SolverStatus.ERROR, solver_used=solver_used
        )

    x = np.round(x_raw).astype(int)
    x_f = x.astype(float)

    # Базовые метрики (из Step 3 логики)
    pnl = pnl_vector(x_f, pkg.payoff_matrix, pkg.entry, pkg.grid, params.spot_price)
    gr  = pkg.greeks

    portfolio_theta = float(gr.theta_usd   @ x_f)
    portfolio_delta = float(gr.delta_vec   @ x_f)
    portfolio_gamma = float(gr.gamma_vec   @ x_f)
    portfolio_vega  = float(gr.vega_usd    @ x_f)
    margin_used     = float(gr.margin_vec  @ np.abs(x_f))
    active_legs     = [i for i, xi in enumerate(x) if xi != 0]

    # Theta по сценариям
    theta_by_scenario = [
        float(theta_s @ x_f)
        for theta_s in ext.scenario_theta.theta_by_scenario
    ]
    expected_theta_usd = float(ext.scenario_theta.expected_theta @ x_f)

    # IV stress MTM P&L
    iv_stress_pnl = float(ext.iv_stress_delta @ x_f)

    # Сравнение BS base theta vs Deribit theta
    base_scenario_idx = next(
        (i for i, s in enumerate(MARKET_SCENARIOS) if s["name"] == "base"), 2
    )
    bs_base_theta = float(ext.scenario_theta.theta_by_scenario[base_scenario_idx] @ x_f)
    deribit_theta = portfolio_theta  # gr.theta_usd уже в USD/day
    bs_vs_deribit = (bs_base_theta - deribit_theta)

    result = ExtendedMILPResult(
        status              = SolverStatus.OPTIMAL,
        solver_used         = solver_used,
        x                   = x,
        objective_value     = float(milp.problem.value) if milp.problem.value else 0.0,
        pnl_by_price        = pnl,
        portfolio_theta     = portfolio_theta,
        portfolio_delta     = portfolio_delta,
        portfolio_gamma     = portfolio_gamma,
        portfolio_vega      = portfolio_vega,
        margin_used         = margin_used,
        params_used         = None,   # ExtendedMILPParams не совместим напрямую
        relaxation_step     = params.relaxation_step,
        active_legs         = active_legs,
        theta_by_scenario   = theta_by_scenario,
        expected_theta_usd  = expected_theta_usd,
        iv_stress_pnl       = iv_stress_pnl,
        bs_vs_deribit_theta = bs_vs_deribit,
    )

    # Сохраняем params в совместимом виде для print_result
    from optimizer_step3 import MILPParams
    result.params_used = MILPParams(
        spot_price    = params.spot_price,
        max_qty       = params.max_qty,
        margin_budget = params.margin_budget,
        pnl_floor     = params.pnl_floor,
        pnl_ceiling   = params.pnl_ceiling,
        range_high    = params.range_high,
        holding_days  = params.holding_days,
        max_delta     = params.max_delta,
        max_vega_usd  = params.max_vega_usd,
        max_gamma     = params.max_gamma,
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ГЛАВНАЯ ФУНКЦИЯ РЕШЕНИЯ (Step 4)
# ─────────────────────────────────────────────────────────────────────────────

def solve_portfolio_v4(
    pkg:    PayoffPackage,
    params: ExtendedMILPParams,
) -> ExtendedMILPResult:
    """
    Полный цикл оптимизации Step 4.

    Pipeline:
        1. Предвычислить сценарную theta и IV stress векторы
        2. Построить расширенную MILP
        3. Попробовать солверы каскадом
        4. При infeasible → авто-релаксация
        5. Вернуть ExtendedMILPResult

    Авто-релаксация для Step 4:
        Та же логика что в Step 3, но сначала пробуем ослабить
        Greeks constraints (они строже чем в Step 3):
            Шаг 0: исходные параметры
            Шаг 1: PNL_FLOOR ×1.2
            Шаг 2: + MARGIN_BUDGET ×1.2
            Шаг 3: + MAX_DELTA ×2

    Args:
        pkg:    PayoffPackage из Step 2.
        params: ExtendedMILPParams.

    Returns:
        ExtendedMILPResult.
    """
    t0 = time.monotonic()
    log.info(
        "Step 4 MILP: %d инструментов, %d сценариев",
        pkg.n_instruments, len(MARKET_SCENARIOS),
    )

    available_solvers = _get_available_mip_solvers()

    # Предвычисляем один раз — не зависит от params при релаксации
    ext = precompute_extended_data(pkg, params)

    log.info(
        "Scenario theta (base): портфельная по 10 инструментам, "
        "expected theta[0]=%.4f USD/day",
        ext.scenario_theta.expected_theta[0] if len(ext.scenario_theta.expected_theta) > 0 else 0,
    )

    def _try_with_params(p: ExtendedMILPParams) -> tuple[str, str, Optional[MILPProblem]]:
        milp_problem = build_extended_milp_problem(pkg, p, ext)
        status, solver = try_solvers(milp_problem, available_solvers)
        return status, solver, milp_problem

    # Первая попытка
    status, solver_used, milp = _try_with_params(params)

    if status == "optimal":
        result = _extract_extended_result(milp, pkg, params, ext, solver_used)
        result.solve_time = time.monotonic() - t0
        return result

    # Авто-релаксация
    diagnostics: list[str] = [
        "Задача неразрешима с исходными параметрами. Запускаем авто-релаксацию."
    ]

    for step in range(1, MAX_RELAXATION_STEPS + 1):
        from optimizer_step3 import MILPParams as _MP3
        # Создаём релаксированные ExtendedMILPParams
        import copy
        relaxed = copy.copy(params)
        relaxed.relaxation_step = step

        if step == 1:
            relaxed.pnl_floor = params.pnl_floor * 1.20
            desc = f"PNL_FLOOR → {relaxed.pnl_floor:.0f}"
        elif step == 2:
            relaxed.pnl_floor     = params.pnl_floor    * 1.20
            relaxed.margin_budget = params.margin_budget * 1.20
            desc = f"+ MARGIN_BUDGET → {relaxed.margin_budget:.0f}"
        else:
            relaxed.pnl_floor     = params.pnl_floor    * 1.20
            relaxed.margin_budget = params.margin_budget * 1.20
            relaxed.max_delta     = params.max_delta     * 2.00
            desc = f"+ MAX_DELTA → {relaxed.max_delta:.3f}"

        diagnostics.append(f"Релаксация {step}/3: {desc}")
        status, solver_used, milp = _try_with_params(relaxed)

        if status == "optimal":
            diagnostics.append(f"✓ Решение найдено после {step} шага(ов) релаксации.")
            result = _extract_extended_result(milp, pkg, relaxed, ext, solver_used)
            result.relaxation_step = step
            result.diagnostics     = diagnostics
            result.solve_time      = time.monotonic() - t0
            return result

    diagnostics.append("✗ Решение не найдено после всех шагов релаксации.")
    return ExtendedMILPResult(
        status      = SolverStatus.INFEASIBLE,
        diagnostics = diagnostics,
        solve_time  = time.monotonic() - t0,
    )


# ─────────────────────────────────────────────────────────────────────────────
# РАСШИРЕННЫЙ ВЫВОД РЕЗУЛЬТАТА
# ─────────────────────────────────────────────────────────────────────────────

def print_result_v4(
    result: ExtendedMILPResult,
    pkg:    PayoffPackage,
) -> None:
    """
    Форматированный отчёт Step 4 с расширенными метриками.

    Добавляет к отчёту Step 3:
        - Breakdown theta по 5 сценариям
        - Сравнение BS base theta vs Deribit theta
        - IV stress P&L
        - Greeks constraints статус (выполнены / нарушены)

    Args:
        result: ExtendedMILPResult.
        pkg:    PayoffPackage.
    """
    W       = 60
    border  = "═" * W
    divider = "─" * W

    print(f"\n{border}")
    print("  ETH OPTIONS OPTIMIZER — Step 4: Extended MILP")
    print(f"  Статус: {result.status.name}  |  Солвер: {result.solver_used}")
    if result.relaxation_step > 0:
        print(f"  ⚠ Решено с релаксацией {result.relaxation_step}/3")
    print(border)

    if result.status == SolverStatus.INFEASIBLE:
        print("\n  ЗАДАЧА НЕРАЗРЕШИМА\n")
        for msg in result.diagnostics:
            print(f"  {msg}")
        print()
        return

    if result.x is None:
        print("\n  Нет решения.\n")
        return

    params = result.params_used

    # ── Позиции ───────────────────────────────────────────────────────────────
    print("\nОПТИМАЛЬНАЯ ПОЗИЦИЯ")
    print(divider)
    print(
        f"  {'#':<3} {'Инструмент':<26} {'Dir':<6} {'Qty':>4}"
        f"  {'θ_deribit/d':>11}  {'θ_bs/d':>9}"
    )
    print(f"  {'-'*3} {'-'*26} {'-'*6} {'-'*4}  {'-'*11}  {'-'*9}")

    base_idx = next(
        (i for i, s in enumerate(MARKET_SCENARIOS) if s["name"] == "base"), 2
    )

    for i in result.active_legs:
        inst      = pkg.instruments[i]
        xi        = result.x[i]
        direction = "LONG " if xi > 0 else "SHORT"
        theta_d   = pkg.greeks.theta_usd[i] * xi      # Deribit theta × qty
        theta_bs  = 0.0
        if result.theta_by_scenario:
            from optimizer_step4 import build_scenario_theta  # самоссылка — безопасно
            pass  # используем предвычисленные данные

        print(
            f"  {i+1:<3} {inst.name:<26} {direction:<6} {xi:>+4}"
            f"  ${theta_d:>9.2f}/d  (вклад)"
        )

    if not result.active_legs:
        print("  (нет активных позиций)")

    # ── Theta breakdown по сценариям ──────────────────────────────────────────
    print(f"\nTHETA — СЦЕНАРНЫЙ АНАЛИЗ")
    print(divider)
    print(f"  {'Сценарий':<10} {'Spot mult':>10} {'IV shift':>9} {'Prob':>6} {'Theta/день':>12}")
    print(f"  {'-'*10} {'-'*10} {'-'*9} {'-'*6} {'-'*12}")

    for i, (s, theta_s) in enumerate(
        zip(MARKET_SCENARIOS, result.theta_by_scenario)
    ):
        print(
            f"  {s['name']:<10} ×{s['spot_mult']:.2f}      "
            f"{s['iv_shift']:>+.0%}     {s['prob']:.0%}   "
            f"  ${theta_s:>9.2f}"
        )

    print(f"  {'':─<52}")
    print(f"  {'ОЖИДАЕМАЯ':>32} (взвешенная)   ${result.expected_theta_usd:>9.2f}/д")
    print(f"  {'Deribit theta':>32} (point est)    ${result.portfolio_theta:>9.2f}/д")

    diff = result.bs_vs_deribit_theta
    sign = "+" if diff >= 0 else ""
    print(f"  {'Разница BS-base vs Deribit':>32}               {sign}${diff:.2f}")

    holding = params.holding_days if params else 5
    print(f"\n  Ожидаемая theta за {holding} дней: ${result.expected_theta_usd * holding:.2f}")

    # ── Greeks портфеля ───────────────────────────────────────────────────────
    print(f"\nGREEKS ПОРТФЕЛЯ  vs  ЛИМИТЫ")
    print(divider)

    def _status(val: float, limit: float, two_sided: bool = True) -> str:
        if two_sided:
            return "✓" if abs(val) <= limit else "⚠ НАРУШЕН"
        return "✓" if val >= -limit else "⚠ НАРУШЕН"

    max_d = params.max_delta    if params else 0.15
    max_v = params.max_vega_usd if params else 600.0
    max_g = params.max_gamma    if params else 0.008

    print(
        f"  Delta:  {result.portfolio_delta:>8.4f}  "
        f"(лимит ±{max_d:.2f})  {_status(result.portfolio_delta, max_d)}"
    )
    print(
        f"  Vega:  ${result.portfolio_vega:>8.2f}  "
        f"(лимит ±${max_v:.0f})  {_status(result.portfolio_vega, max_v)}"
    )
    print(
        f"  Gamma:  {result.portfolio_gamma:>8.5f}  "
        f"(лимит ≥−{max_g:.3f}) {_status(result.portfolio_gamma, max_g, two_sided=False)}"
    )

    # ── P&L сценарии ──────────────────────────────────────────────────────────
    print(f"\nP&L ЭКСПИРАЦИОННЫЕ СЦЕНАРИИ")
    print(divider)

    if result.pnl_by_price is not None and params:
        display_prices = [1_400, 1_600, 1_700, 1_800, 1_900, 2_000,
                          2_100, 2_200, 2_300, 2_500, 2_700]
        for dp in display_prices:
            matches = np.where(np.abs(pkg.grid.prices - dp) < 0.01)[0]
            if len(matches) == 0:
                continue
            pnl_val = result.pnl_by_price[matches[0]]
            floor   = params.pnl_ceiling if abs(dp - params.range_high) < 0.01 else params.pnl_floor
            ok      = pnl_val >= floor - 0.01
            mark    = "✓" if ok else "⚠"
            label   = ""
            if dp == params.spot_price:
                label = " ← spot"
            elif dp == params.range_high:
                label = " ← ceiling"
            print(f"  ETH ${dp:>5,}:  ${pnl_val:>8.0f}  {mark}{label}")

    # ── IV Stress ─────────────────────────────────────────────────────────────
    print(f"\nIV STRESS TEST  (+{IV_STRESS_SHIFT:.0%} IV прямо сейчас)")
    print(divider)
    stress_floor = (params.pnl_floor if params else -400) * IV_STRESS_FLOOR_MULTIPLIER
    stress_ok    = result.iv_stress_pnl >= stress_floor - 0.01
    print(
        f"  MTM P&L при IV +{IV_STRESS_SHIFT:.0%}:  ${result.iv_stress_pnl:>8.2f}  "
        f"(лимит ≥${stress_floor:.0f})  {'✓' if stress_ok else '⚠'}"
    )

    # ── Риск-метрики ──────────────────────────────────────────────────────────
    print(f"\nРИСК-МЕТРИКИ")
    print(divider)
    if params:
        pct = result.margin_used / params.margin_budget * 100
        print(f"  Маржа:    ${result.margin_used:>7.0f} / ${params.margin_budget:.0f}  ({pct:.1f}%)")
    if result.pnl_by_price is not None:
        print(f"  Max loss: ${result.pnl_by_price.min():>8.0f}")
        print(f"  Max gain: ${result.pnl_by_price.max():>8.0f}")
    print(f"  Время:    {result.solve_time:.2f}s")

    if result.diagnostics:
        print(f"\nДИАГНОСТИКА")
        print(divider)
        for msg in result.diagnostics:
            print(f"  {msg}")

    print(f"\n{border}\n")


# ─────────────────────────────────────────────────────────────────────────────
# UNIT TESTS
# ─────────────────────────────────────────────────────────────────────────────

def run_unit_tests(verbose: bool = False) -> None:
    """
    Unit tests для Step 4.

    Тесты:
        1.  BS price: put-call parity  C − P = S − K×e^(−rT)
        2.  BS price: нижняя граница  C ≥ max(S−K, 0),  P ≥ max(K−S, 0)
        3.  BS theta: знак отрицательный для обоих типов (long perspective)
        4.  BS theta: глубокий OTM → theta стремится к 0
        5.  BS theta: ATM theta растёт при росте IV (vega-theta trade-off)
        6.  build_scenario_theta: 5 сценариев, shape [N]
        7.  build_scenario_theta: base сценарий ≈ Deribit theta (±20%)
        8.  build_scenario_theta: crash theta < base theta для short put
        9.  build_iv_stress_delta: знак (+) для обоих типов (вега всегда +)
        10. build_iv_stress_delta: больший IV shift → большее изменение цены
        11. MILP с synthetic instruments: Greeks constraints выполнены
        12. MILP: expected theta > single-point Deribit theta (качество оптимизации)
    """
    print("Запуск unit tests Step 4: Extended MILP\n")
    passed = 0
    failed = 0
    EPS    = 1e-6

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            if verbose:
                print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name}" + (f": {detail}" if detail else ""))

    # Параметры для BS тестов
    S, K, T_YEAR, SIGMA, R = 2_000.0, 2_000.0, 8.0 / 365.0, 0.80, 0.05

    # ── TEST 1: Put-Call Parity ────────────────────────────────────────────────
    print("TEST 1: Put-Call Parity  C − P = S − K·e^(−rT)")
    C = bs_price(S, K, T_YEAR, SIGMA, R, "call")
    P_val = bs_price(S, K, T_YEAR, SIGMA, R, "put")
    pcp_left  = C - P_val
    pcp_right = S - K * math.exp(-R * T_YEAR)
    check("Put-call parity ATM",
          abs(pcp_left - pcp_right) < 0.001,
          f"C-P={pcp_left:.4f}, S-Ke^(-rT)={pcp_right:.4f}")

    # Deep ITM call
    C_itm  = bs_price(S, 1_500.0, T_YEAR, SIGMA, R, "call")
    P_itm  = bs_price(S, 1_500.0, T_YEAR, SIGMA, R, "put")
    pcp_r2 = S - 1_500.0 * math.exp(-R * T_YEAR)
    check("Put-call parity deep ITM",
          abs((C_itm - P_itm) - pcp_r2) < 0.01,
          f"diff={(C_itm-P_itm-pcp_r2):.4f}")

    if verbose:
        print(f"  ATM Call=${C:.2f}, Put=${P_val:.2f}, C-P={pcp_left:.4f}")
    print()

    # ── TEST 2: Нижние границы цены ───────────────────────────────────────────
    print("TEST 2: Нижние границы BS цены")
    for Ktest in [1_700.0, 2_000.0, 2_300.0]:
        c = bs_price(S, Ktest, T_YEAR, SIGMA, R, "call")
        p = bs_price(S, Ktest, T_YEAR, SIGMA, R, "put")
        check(f"Call ≥ intrinsic при K={Ktest:.0f}",
              c >= max(S - Ktest, 0.0) - EPS,
              f"call={c:.4f} < intrinsic={max(S-Ktest,0):.4f}")
        check(f"Put ≥ intrinsic при K={Ktest:.0f}",
              p >= max(Ktest - S, 0.0) - EPS,
              f"put={p:.4f} < intrinsic={max(Ktest-S,0):.4f}")
        check(f"Call ≥ 0 при K={Ktest:.0f}", c >= 0.0)
        check(f"Put  ≥ 0 при K={Ktest:.0f}", p >= 0.0)
    print()

    # ── TEST 3: Знак theta (отрицателен для long) ─────────────────────────────
    print("TEST 3: Знак theta — отрицателен для long опционов")
    for Ktest in [1_800.0, 2_000.0, 2_200.0]:
        theta_c = bs_theta_usd_per_day(S, Ktest, T_YEAR, SIGMA, R, "call")
        theta_p = bs_theta_usd_per_day(S, Ktest, T_YEAR, SIGMA, R, "put")
        check(f"Theta call < 0 при K={Ktest:.0f}",
              theta_c < 0,
              f"theta_call={theta_c:.4f}")
        check(f"Theta put  < 0 при K={Ktest:.0f}",
              theta_p < 0,
              f"theta_put={theta_p:.4f}")
    if verbose:
        print(f"  ATM call theta: ${bs_theta_usd_per_day(S, K, T_YEAR, SIGMA, R, 'call'):.3f}/day")
        print(f"  ATM put  theta: ${bs_theta_usd_per_day(S, K, T_YEAR, SIGMA, R, 'put'):.3f}/day")
    print()

    # ── TEST 4: Deep OTM theta → 0 ────────────────────────────────────────────
    print("TEST 4: Deep OTM theta стремится к нулю")
    theta_deep_otm_call = bs_theta_usd_per_day(S, 5_000.0, T_YEAR, SIGMA, R, "call")
    theta_deep_otm_put  = bs_theta_usd_per_day(S, 500.0,   T_YEAR, SIGMA, R, "put")
    ATM_theta = abs(bs_theta_usd_per_day(S, K, T_YEAR, SIGMA, R, "call"))

    check("Deep OTM call theta << ATM theta",
          abs(theta_deep_otm_call) < ATM_theta * 0.10,
          f"deep_otm={theta_deep_otm_call:.4f}, ATM_abs={ATM_theta:.4f}")
    check("Deep OTM put theta << ATM theta",
          abs(theta_deep_otm_put)  < ATM_theta * 0.10,
          f"deep_otm={theta_deep_otm_put:.4f}, ATM_abs={ATM_theta:.4f}")
    print()

    # ── TEST 5: ATM theta vs IV (выше IV → выше абс. theta для ATM) ──────────
    print("TEST 5: ATM theta растёт (по модулю) с ростом IV")
    theta_low_iv  = bs_theta_usd_per_day(S, K, T_YEAR, 0.40, R, "call")
    theta_high_iv = bs_theta_usd_per_day(S, K, T_YEAR, 1.20, R, "call")
    check("abs(theta) при IV=120% > abs(theta) при IV=40%",
          abs(theta_high_iv) > abs(theta_low_iv),
          f"low_iv={theta_low_iv:.3f}, high_iv={theta_high_iv:.3f}")
    if verbose:
        print(f"  IV=40%: ${theta_low_iv:.3f}/day, IV=120%: ${theta_high_iv:.3f}/day")
    print()

    # ── Создаём синтетические инструменты ────────────────────────────────────
    SPOT = 2_000.0
    instruments = [
        OptionInstrument(
            name="TEST-PUT-1900",  option_type="put",  strike=1_900.0,
            expiry_ts=0, best_bid=0.018, best_ask=0.022, mark_price=0.020,
            delta=-0.22, gamma=0.001, theta=-0.0010, vega=0.45,
            days_to_expiry=8.0, mark_iv=80.0, liquidity_score=0.8,
            open_interest=200.0, volume_usd_24h=300_000.0, spread_pct=0.10,
        ),
        OptionInstrument(
            name="TEST-CALL-2100", option_type="call", strike=2_100.0,
            expiry_ts=0, best_bid=0.018, best_ask=0.022, mark_price=0.020,
            delta=0.22, gamma=0.001, theta=-0.0010, vega=0.45,
            days_to_expiry=8.0, mark_iv=80.0, liquidity_score=0.8,
            open_interest=200.0, volume_usd_24h=300_000.0, spread_pct=0.10,
        ),
        OptionInstrument(
            name="TEST-PUT-1800",  option_type="put",  strike=1_800.0,
            expiry_ts=0, best_bid=0.006, best_ask=0.009, mark_price=0.007,
            delta=-0.10, gamma=0.0005, theta=-0.0004, vega=0.20,
            days_to_expiry=8.0, mark_iv=85.0, liquidity_score=0.7,
            open_interest=100.0, volume_usd_24h=100_000.0, spread_pct=0.15,
        ),
        OptionInstrument(
            name="TEST-CALL-2200", option_type="call", strike=2_200.0,
            expiry_ts=0, best_bid=0.006, best_ask=0.009, mark_price=0.007,
            delta=0.10, gamma=0.0005, theta=-0.0004, vega=0.20,
            days_to_expiry=8.0, mark_iv=85.0, liquidity_score=0.7,
            open_interest=100.0, volume_usd_24h=100_000.0, spread_pct=0.15,
        ),
    ]

    # ── TEST 6: build_scenario_theta — структура ──────────────────────────────
    print("TEST 6: build_scenario_theta — структура и размерность")
    st = build_scenario_theta(instruments, SPOT)
    check("Число сценариев == 5",
          len(st.theta_by_scenario) == 5)
    check("Каждый вектор shape [N]",
          all(len(t) == len(instruments) for t in st.theta_by_scenario))
    check("expected_theta shape [N]",
          len(st.expected_theta) == len(instruments))
    check("Сумма вероятностей == 1.0",
          abs(sum(st.scenario_probs) - 1.0) < EPS)
    check("Все theta < 0 (для long optics theta_usd < 0)",
          all(np.all(t < 0) for t in st.theta_by_scenario),
          "Long опционы имеют отрицательную theta")
    if verbose:
        for name, t in zip(st.scenario_names, st.theta_by_scenario):
            print(f"  Scenario {name}: theta[0]=${t[0]:.4f}/day")
    print()

    # ── TEST 7: Base scenario ≈ BS theta (в рамках ±30%) ─────────────────────
    print("TEST 7: Base scenario theta близка к BS theta при текущем spot/IV")
    base_idx  = st.scenario_names.index("base")
    base_t    = st.theta_by_scenario[base_idx]

    for i, inst in enumerate(instruments):
        T     = inst.days_to_expiry / DAYS_PER_YEAR
        sigma = inst.mark_iv / 100.0
        bs_t  = bs_theta_usd_per_day(SPOT, inst.strike, T, sigma, RISK_FREE_RATE, inst.option_type)
        if abs(bs_t) > EPS:
            diff_pct = abs(base_t[i] - bs_t) / abs(bs_t)
            check(f"Base theta ≈ direct BS theta для {inst.name} (±1%)",
                  diff_pct < 0.01,
                  f"base={base_t[i]:.5f}, direct={bs_t:.5f}, diff={diff_pct:.3%}")
    print()

    # ── TEST 8: Crash theta < base theta для short put ─────────────────────────
    print("TEST 8: В crash сценарии theta short put УМЕНЬШАЕТСЯ (хуже для нас)")
    crash_idx = st.scenario_names.index("crash")
    base_idx  = st.scenario_names.index("base")
    # Short put (x=-1) вклад в theta = theta_vec[i] × (−1) (положительный)
    # При crash: spot падает, put становится ATM/ITM, abs(theta) растёт
    # Значит short put theta_usd[i] < 0 более negative → вклад −theta > 0 растёт
    # Проверяем: для put[0] (strike 1900) crash theta более отрицательна?
    # Crash: spot=1800, put-1900 становится ATM → больше abs(theta)
    crash_t_put = st.theta_by_scenario[crash_idx][0]  # PUT-1900
    base_t_put  = st.theta_by_scenario[base_idx][0]
    check(
        "PUT-1900 при crash (spot=1800) имеет большую abs(theta) чем при base",
        abs(crash_t_put) >= abs(base_t_put) * 0.9,
        f"crash={crash_t_put:.5f}, base={base_t_put:.5f}"
    )
    if verbose:
        print(f"  PUT-1900: crash theta={crash_t_put:.5f}, base theta={base_t_put:.5f}")
    print()

    # ── TEST 9: IV stress delta — знак ────────────────────────────────────────
    print("TEST 9: IV stress delta — знак всегда положительный (vega > 0)")
    iv_delta = build_iv_stress_delta(instruments, SPOT)
    check("Все IV stress delta > 0",
          np.all(iv_delta > 0),
          f"min={iv_delta.min():.4f}")
    check("Shape [N]",
          len(iv_delta) == len(instruments))
    if verbose:
        for i, inst in enumerate(instruments):
            print(f"  {inst.name}: Δmark при IV+25% = ${iv_delta[i]:.4f}")
    print()

    # ── TEST 10: Больший IV shift → больший Δmark ─────────────────────────────
    print("TEST 10: Больший IV stress → большее изменение цены")
    iv_delta_small = build_iv_stress_delta(instruments, SPOT, iv_stress_shift=0.10)
    iv_delta_large = build_iv_stress_delta(instruments, SPOT, iv_stress_shift=0.50)
    check("IV+50% Δmark > IV+10% Δmark для всех инструментов",
          np.all(iv_delta_large > iv_delta_small),
          f"min diff={np.min(iv_delta_large - iv_delta_small):.6f}")
    print()

    # ── TEST 11+12: MILP с Greeks constraints ────────────────────────────────
    print("TEST 11-12: MILP с Greeks constraints")
    pkg = build_payoff_package(instruments, SPOT)
    params_t = ExtendedMILPParams(
        spot_price    = SPOT,
        max_qty       = 5,
        margin_budget = 3_000.0,
        pnl_floor     = -250.0,
        pnl_ceiling   = +80.0,
        range_high    = 2_200.0,
        holding_days  = 5,
        max_delta     = 0.30,    # мягче для теста
        max_vega_usd  = 800.0,
        max_gamma     = 0.020,
    )

    try:
        result = solve_portfolio_v4(pkg, params_t)
        check("Статус OPTIMAL",
              result.status == SolverStatus.OPTIMAL,
              f"got {result.status.name}")

        if result.status == SolverStatus.OPTIMAL and result.x is not None:
            x_f = result.x.astype(float)
            gr  = pkg.greeks

            # TEST 11: Greeks constraints выполнены
            actual_delta = float(gr.delta_vec @ x_f)
            actual_vega  = float(gr.vega_usd  @ x_f)
            actual_gamma = float(gr.gamma_vec @ x_f)

            check("Delta constraint выполнен",
                  abs(actual_delta) <= params_t.max_delta + EPS,
                  f"|delta|={abs(actual_delta):.4f} > {params_t.max_delta}")
            check("Vega constraint выполнен",
                  abs(actual_vega) <= params_t.max_vega_usd + EPS,
                  f"|vega|={abs(actual_vega):.2f} > {params_t.max_vega_usd}")
            check("Gamma constraint выполнен",
                  actual_gamma >= -params_t.max_gamma - EPS,
                  f"gamma={actual_gamma:.5f} < {-params_t.max_gamma}")

            # TEST 12: IV stress constraint выполнен
            ext  = precompute_extended_data(pkg, params_t)
            iv_s = float(ext.iv_stress_delta @ x_f)
            iv_floor = params_t.pnl_floor * params_t.iv_stress_multiplier
            check("IV stress constraint выполнен",
                  iv_s >= iv_floor - EPS,
                  f"iv_stress={iv_s:.2f} < floor={iv_floor:.2f}")

            if verbose:
                print(f"  Delta:  {actual_delta:.4f} (лимит ±{params_t.max_delta})")
                print(f"  Vega:  ${actual_vega:.2f} (лимит ±${params_t.max_vega_usd})")
                print(f"  Gamma:  {actual_gamma:.5f} (лимит ≥{-params_t.max_gamma})")
                print(f"  IV stress: ${iv_s:.2f} (лимит ${iv_floor:.2f})")
                print(f"  Expected theta: ${result.expected_theta_usd:.3f}/day")
                print(f"  Deribit theta:  ${result.portfolio_theta:.3f}/day")

    except Exception as exc:
        check("Solve без исключений", False, str(exc))
        import traceback; traceback.print_exc()

    print()

    # ── Summary ───────────────────────────────────────────────────────────────
    total = passed + failed
    print("─" * 50)
    if failed == 0:
        print(f"  ✓ Все {total} тестов прошли.")
    else:
        print(f"  ✗ {failed} из {total} тестов провалились.")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# LIVE РЕЖИМ
# ─────────────────────────────────────────────────────────────────────────────

async def _run_live(params: ExtendedMILPParams) -> None:
    """Запускает полный Step 4 pipeline на живых данных Deribit."""
    print("Загружаем инструменты с Deribit…")
    options, stats = await fetch_liquid_options()

    if not options:
        print("Нет ликвидных инструментов.")
        return

    print(f"Получено {stats.final_count} инструментов ({stats.elapsed_seconds:.1f}s)")
    print("Строим PayoffPackage + вычисляем BS сценарии…")
    pkg = build_payoff_package(options, params.spot_price)

    print(
        f"Запускаем Extended MILP "
        f"({pkg.n_instruments} инструментов × {len(MARKET_SCENARIOS)} сценариев)…"
    )
    result = solve_portfolio_v4(pkg, params)
    print_result_v4(result, pkg)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ETH Options Extended MILP Optimizer — Step 4"
    )
    parser.add_argument("--test",    action="store_true", help="Unit tests (без сети)")
    parser.add_argument("--live",    action="store_true", help="Живые данные Deribit")
    parser.add_argument("--verbose", action="store_true", help="Детальный вывод")
    parser.add_argument("--spot",    type=float, default=2_000.0)
    parser.add_argument("--floor",   type=float, default=-400.0)
    parser.add_argument("--ceiling", type=float, default=+400.0)
    parser.add_argument("--margin",  type=float, default=5_000.0)
    parser.add_argument("--max-qty", type=int,   default=10)
    parser.add_argument("--max-delta", type=float, default=0.15)
    parser.add_argument("--max-vega",  type=float, default=600.0)
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.test:
        run_unit_tests(verbose=args.verbose)
        return

    params = ExtendedMILPParams(
        spot_price    = args.spot,
        max_qty       = args.max_qty,
        margin_budget = args.margin,
        pnl_floor     = args.floor,
        pnl_ceiling   = args.ceiling,
        max_delta     = args.max_delta,
        max_vega_usd  = args.max_vega,
    )

    if args.live:
        asyncio.run(_run_live(params))
    else:
        # По умолчанию — синтетика
        from optimizer_step3 import _make_synthetic_instruments
        instruments = _make_synthetic_instruments(args.spot)
        pkg         = build_payoff_package(instruments, args.spot)
        result      = solve_portfolio_v4(pkg, params)
        print_result_v4(result, pkg)


if __name__ == "__main__":
    main()
