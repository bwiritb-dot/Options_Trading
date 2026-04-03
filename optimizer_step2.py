"""
ETH Options Optimizer — Step 2: Payoff Engine
==============================================
Builds the price-scenario grid, payoff matrix, and P&L vectors
used by the MILP solver in Step 3+.

Key design decisions:
    - P&L constraints use EXPIRATION payoff (intrinsic value).
      This is conservative: guarantees risk limits hold even if
      position is held to expiry. Time value adds buffer, never removes it.
    - Entry cost uses REALISTIC bid/ask split, not mid-price.
      Buyers pay ask, sellers receive bid.
    - All USD values assume 1 contract = 1 ETH.
      USD amounts = ETH amounts × SPOT_PRICE.

Standalone usage:
    python optimizer_step2.py             # unit tests (no network)
    python optimizer_step2.py --verbose   # detailed test output

Dependencies:
    pip install numpy
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np
import numpy.typing as npt

# Импортируем модели данных из Шага 1
# В production это будет: from optimizer_step1 import OptionInstrument
# Здесь воспроизводим только то, что нужно Payoff Engine
from dataclasses import field

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

# Сеточные параметры для ценовых сценариев
GRID_START: int = 1_700           # нижняя граница основной сетки, USD
GRID_STOP: int  = 2_401           # верхняя граница (не включается в arange)
GRID_STEP: int  = 25              # шаг сетки, USD

# Стресс-точки для хвостовых рисков (tail risk)
STRESS_POINTS: list[int] = [1_400, 1_500, 1_600, 2_500, 2_600, 2_700]

# Базовые параметры позиции
SPOT_PRICE: float = 2_000.0       # текущая цена ETH в USD (обновляется из API)
ETH_PER_CONTRACT: float = 1.0     # Deribit: 1 контракт = 1 ETH

# Тип опциона
OPTION_TYPE_CALL: str = "call"
OPTION_TYPE_PUT:  str = "put"

# ─────────────────────────────────────────────────────────────────────────────
# DATA MODEL (минимальный интерфейс для Payoff Engine)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OptionInstrument:
    """
    Минимальный контракт данных, необходимый Payoff Engine.
    Полная версия — в optimizer_step1.py.
    """
    name: str
    option_type: str      # "call" | "put"
    strike: float         # USD
    best_bid: float       # ETH-denominated premium
    best_ask: float       # ETH-denominated premium
    mark_price: float     # ETH-denominated
    delta: float
    gamma: float
    theta: float          # ETH/day — multiply by SPOT for USD/day
    vega: float           # ETH per 1% IV
    days_to_expiry: float
    mark_iv: float
    liquidity_score: float
    open_interest: float
    volume_usd_24h: float
    spread_pct: float
    settlement_currency: str = "ETH"
    expiry_ts: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# МОДУЛЬ 2.1 — ЦЕНОВАЯ СЕТКА
# ─────────────────────────────────────────────────────────────────────────────

class PriceGrid(NamedTuple):
    """
    Иммутабельная сетка ценовых сценариев для P&L расчётов.

    Attributes:
        prices:       Отсортированный массив цен в USD, shape [M].
        n_primary:    Количество точек из основной сетки.
        n_stress:     Количество стресс-точек.
        spot_index:   Индекс ближайшей к SPOT_PRICE точки (для отчётов).
    """
    prices:     npt.NDArray[np.float64]
    n_primary:  int
    n_stress:   int
    spot_index: int


def build_price_grid(spot_price: float = SPOT_PRICE) -> PriceGrid:
    """
    Строит объединённую сетку основных + стресс-точек.

    Основная сетка: np.arange(1700, 2401, 25) = 29 точек.
    Стресс-точки: [1400, 1500, 1600, 2500, 2600, 2700] = 6 точек.
    Итого: до 35 уникальных точек (если нет пересечений).

    Args:
        spot_price: Текущая цена ETH в USD — для поиска spot_index.

    Returns:
        PriceGrid с отсортированными уникальными ценами.
    """
    primary = set(np.arange(GRID_START, GRID_STOP, GRID_STEP).tolist())
    stress  = set(STRESS_POINTS)

    all_prices = sorted(primary | stress)
    prices_arr = np.array(all_prices, dtype=np.float64)

    n_primary = len(primary)
    n_stress  = len(stress - primary)  # только уникальные стресс-точки

    # Индекс ближайшей к spot_price точки сетки
    spot_index = int(np.argmin(np.abs(prices_arr - spot_price)))

    return PriceGrid(
        prices     = prices_arr,
        n_primary  = n_primary,
        n_stress   = n_stress,
        spot_index = spot_index,
    )


# ─────────────────────────────────────────────────────────────────────────────
# МОДУЛЬ 2.2 — МАТРИЦА PAYOFF (intrinsic value при экспирации)
# ─────────────────────────────────────────────────────────────────────────────

def _call_payoff(spot: float, strike: float) -> float:
    """Payoff колла при экспирации: max(S - K, 0)."""
    return max(spot - strike, 0.0)


def _put_payoff(spot: float, strike: float) -> float:
    """Payoff пута при экспирации: max(K - S, 0)."""
    return max(strike - spot, 0.0)


def build_payoff_matrix(
    grid: PriceGrid,
    instruments: list[OptionInstrument],
) -> npt.NDArray[np.float64]:
    """
    Строит матрицу payoff при экспирации в ETH.

    P[j, i] = payoff i-го инструмента при цене grid.prices[j] ETH.

    Shape: [M, N] где M = len(grid.prices), N = len(instruments).

    Payoff в ETH (не USD), потому что:
        - Deribit деноминирует premiums в ETH
        - Перевод в USD: P_usd = P_eth × spot_price
        - spot_price изменяется по сетке → умножение происходит в pnl_vector()

    Args:
        grid:        Ценовая сетка из build_price_grid().
        instruments: Список отфильтрованных опционов.

    Returns:
        ndarray shape [M, N], dtype float64.
    """
    M = len(grid.prices)
    N = len(instruments)
    P = np.empty((M, N), dtype=np.float64)

    for i, inst in enumerate(instruments):
        for j, S in enumerate(grid.prices):
            if inst.option_type == OPTION_TYPE_CALL:
                P[j, i] = _call_payoff(S, inst.strike)
            elif inst.option_type == OPTION_TYPE_PUT:
                P[j, i] = _put_payoff(S, inst.strike)
            else:
                raise ValueError(
                    f"Unknown option_type '{inst.option_type}' "
                    f"for instrument '{inst.name}'"
                )

    return P


# ─────────────────────────────────────────────────────────────────────────────
# МОДУЛЬ 2.3 — ВЕКТОРЫ ВХОДНЫХ ЦЕН (bid/ask split)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EntryVectors:
    """
    Векторы реалистичных цен входа для каждого инструмента.

    ask_vec[i]:  цена покупки i-го инструмента (ETH) — лонг платит ask
    bid_vec[i]:  цена продажи i-го инструмента (ETH) — шорт получает bid
    mid_vec[i]:  mid-price (только для информации, не для расчётов)
    """
    ask_vec: npt.NDArray[np.float64]
    bid_vec: npt.NDArray[np.float64]
    mid_vec: npt.NDArray[np.float64]


def build_entry_vectors(
    instruments: list[OptionInstrument],
) -> EntryVectors:
    """
    Извлекает bid/ask/mid векторы из списка инструментов.

    Почему не mid-price:
        Использование mid создаёт иллюзию прибыли. В реальности покупатель
        платит ask, продавец получает bid. Спред — реальные транзакционные
        издержки, которые должны быть учтены в P&L.

    Args:
        instruments: Список опционов с полями best_bid, best_ask.

    Returns:
        EntryVectors с тремя ndarray shape [N].
    """
    ask_vec = np.array([inst.best_ask for inst in instruments], dtype=np.float64)
    bid_vec = np.array([inst.best_bid for inst in instruments], dtype=np.float64)
    mid_vec = (ask_vec + bid_vec) / 2.0

    return EntryVectors(ask_vec=ask_vec, bid_vec=bid_vec, mid_vec=mid_vec)


# ─────────────────────────────────────────────────────────────────────────────
# МОДУЛЬ 2.4 — P&L ВЕКТОР
# ─────────────────────────────────────────────────────────────────────────────

def compute_entry_cost_usd(
    x: npt.NDArray[np.float64],
    entry: EntryVectors,
    spot_price: float,
) -> float:
    """
    Вычисляет реальную стоимость входа в позицию в USD.

    Логика bid/ask split:
        x[i] > 0 → покупаем → платим ask[i] за контракт
        x[i] < 0 → продаём  → получаем bid[i] за контракт
        x[i] = 0 → нет позиции → нет стоимости

    Реализация через явное разделение на лонг/шорт части избегает
    нелинейности: нельзя просто писать ask if x>0 else bid в матричном виде.

    Формула:
        x_long  = max(x, 0)   (покупки)
        x_short = max(-x, 0)  (продажи)
        cost_eth = ask @ x_long - bid @ x_short
        cost_usd = cost_eth × spot_price

    Args:
        x:          Вектор количества контрактов, shape [N]. Может быть float.
        entry:      Bid/ask векторы.
        spot_price: Текущая цена ETH в USD.

    Returns:
        Чистая стоимость входа в USD. Положительная = мы платим (net debit).
        Отрицательная = мы получаем премию (net credit).
    """
    x_long  = np.maximum(x,  0.0)
    x_short = np.maximum(-x, 0.0)

    cost_eth = entry.ask_vec @ x_long - entry.bid_vec @ x_short
    return float(cost_eth * spot_price)


def pnl_at_price(
    x: npt.NDArray[np.float64],
    price_idx: int,
    payoff_matrix: npt.NDArray[np.float64],
    entry: EntryVectors,
    spot_at_scenario: float,
    entry_spot: float,
) -> float:
    """
    Вычисляет P&L позиции при данной цене ETH при экспирации.

    P&L (USD) = payoff_usd - entry_cost_usd

    где:
        payoff_usd   = (P[price_idx] @ x) × spot_at_scenario
        entry_cost   = compute_entry_cost_usd(x, entry, entry_spot)

    Важно: payoff умножается на spot_at_scenario (цену сценария),
    а стоимость входа — на entry_spot (цену на момент входа).
    Это корректно: мы продали/купили при entry_spot, payoff
    реализуется при spot_at_scenario.

    Args:
        x:                 Вектор позиций, shape [N].
        price_idx:         Индекс ценовой точки в payoff_matrix.
        payoff_matrix:     Матрица P shape [M, N].
        entry:             Bid/ask векторы.
        spot_at_scenario:  Цена ETH в данной ценовой точке (USD).
        entry_spot:        Цена ETH на момент входа в позицию (USD).

    Returns:
        P&L в USD.
    """
    payoff_usd  = float(payoff_matrix[price_idx] @ x)   # already USD
    cost_usd    = compute_entry_cost_usd(x, entry, entry_spot)
    return payoff_usd - cost_usd


def pnl_vector(
    x: npt.NDArray[np.float64],
    payoff_matrix: npt.NDArray[np.float64],
    entry: EntryVectors,
    grid: PriceGrid,
    entry_spot: float,
) -> npt.NDArray[np.float64]:
    """
    Вычисляет P&L для ВСЕХ ценовых точек сетки одновременно.

    Оптимизировано через матричное умножение вместо цикла.
    Используется в MILP для построения P&L constraints.

    Дизайн: payoff_matrix[j, i] = USD intrinsic value при цене S_j.
        Call: max(S_j - K_i, 0)
        Put:  max(K_i - S_j, 0)

    Это КРИТИЧНО для MILP: constraint P[j] @ x >= PNL_FLOOR линеен в x
    только если P[j] — фиксированный вектор коэффициентов. Если бы мы
    хранили ETH-payoff и умножали на переменный спот — constraint стал бы
    нелинейным и MILP не смог бы его решить.

    Формула:
        payoff_usd_vec = P @ x          shape [M]  (уже в USD)
        cost_usd       = scalar
        pnl_vec        = payoff_usd_vec - cost_usd

    Args:
        x:             Вектор позиций, shape [N].
        payoff_matrix: shape [M, N] — USD intrinsic values.
        entry:         Bid/ask векторы.
        grid:          Ценовая сетка (используется только для размера M).
        entry_spot:    Цена ETH на момент входа в позицию (USD).

    Returns:
        ndarray shape [M] — P&L в USD для каждой ценовой точки.
    """
    payoff_usd_vec = payoff_matrix @ x                      # shape [M], уже в USD
    cost_usd       = compute_entry_cost_usd(x, entry, entry_spot)
    return payoff_usd_vec - cost_usd


# ─────────────────────────────────────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ВЕКТОРЫ ДЛЯ MILP
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GreekVectors:
    """
    Векторы греков для всех инструментов — готово для MILP constraints.

    Все значения в USD-совместимых единицах.
    """
    delta_vec:    npt.NDArray[np.float64]   # безразмерный [−1, +1]
    gamma_vec:    npt.NDArray[np.float64]   # 1/ETH
    theta_usd:    npt.NDArray[np.float64]   # USD/день = theta_eth × spot
    vega_usd:     npt.NDArray[np.float64]   # USD на 1% IV
    margin_vec:   npt.NDArray[np.float64]   # USD (≈ mark_price × spot × 1.25)


def build_greek_vectors(
    instruments: list[OptionInstrument],
    spot_price: float,
) -> GreekVectors:
    """
    Строит векторы греков в USD-единицах для MILP.

    Критично для theta:
        Deribit возвращает theta в ETH/день.
        Для P&L в USD: theta_usd[i] = theta_eth[i] × spot_price
        Знак theta: отрицательный для лонгов, положительный для шортов.

    Критично для vega:
        Deribit vega в ETH на 1% IV.
        vega_usd[i] = vega_eth[i] × spot_price

    Маржа (линейная аппроксимация):
        Для шорт-опциона маржа ≈ mark_price × spot × буфер.
        Для лонг-опциона маржа = 0 (заплачена авансом как premium).
        Используем mark_price как прокси для обоих случаев на этапе
        первого MILP — Шаг 5 уточняет через get_margins API.

    Args:
        instruments: Список опционов.
        spot_price:  Текущая цена ETH в USD.

    Returns:
        GreekVectors с ndarray shape [N] для каждого грека.
    """
    delta_vec  = np.array([inst.delta     for inst in instruments], dtype=np.float64)
    gamma_vec  = np.array([inst.gamma     for inst in instruments], dtype=np.float64)
    theta_usd  = np.array([inst.theta * spot_price for inst in instruments], dtype=np.float64)
    vega_usd   = np.array([inst.vega  * spot_price for inst in instruments], dtype=np.float64)

    # Линейная аппроксимация маржи: mark_price (ETH) × spot (USD/ETH) × 1.25 буфер
    # Буфер 25% заложен здесь; итерационное уточнение — Шаг 5
    MARGIN_BUFFER: float = 1.25
    margin_vec = np.array(
        [inst.mark_price * spot_price * MARGIN_BUFFER for inst in instruments],
        dtype=np.float64,
    )

    return GreekVectors(
        delta_vec  = delta_vec,
        gamma_vec  = gamma_vec,
        theta_usd  = theta_usd,
        vega_usd   = vega_usd,
        margin_vec = margin_vec,
    )


# ─────────────────────────────────────────────────────────────────────────────
# СВОДНЫЙ ОБЪЕКТ — всё что нужно MILP из Steps 3+
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PayoffPackage:
    """
    Полный пакет данных для MILP-оптимизатора.

    Передаётся из Step 2 в Step 3 как единый объект.
    Все тяжёлые вычисления (матрица, векторы) сделаны один раз здесь.

    Attributes:
        instruments:    Список опционов в том же порядке, что и столбцы матриц.
        grid:           Ценовая сетка.
        payoff_matrix:  P shape [M, N] в ETH.
        entry:          Bid/ask/mid векторы.
        greeks:         Greeks в USD-единицах.
        spot_price:     Спот-цена на момент сборки пакета.
        n_instruments:  N — количество инструментов.
        n_price_points: M — количество ценовых точек.
    """
    instruments:    list[OptionInstrument]
    grid:           PriceGrid
    payoff_matrix:  npt.NDArray[np.float64]
    entry:          EntryVectors
    greeks:         GreekVectors
    spot_price:     float
    n_instruments:  int
    n_price_points: int


def build_payoff_package(
    instruments: list[OptionInstrument],
    spot_price: float = SPOT_PRICE,
) -> PayoffPackage:
    """
    Точка входа Step 2: строит весь PayoffPackage из списка инструментов.

    Вызывается один раз после fetch из Step 1.

    Args:
        instruments: Отфильтрованные OptionInstrument из Step 1.
        spot_price:  Текущий спот ETH/USD (желательно из order book).

    Returns:
        Полный PayoffPackage для передачи в MILP.

    Raises:
        ValueError: Если instruments пуст.
    """
    if not instruments:
        raise ValueError("Cannot build PayoffPackage: instruments list is empty.")

    grid   = build_price_grid(spot_price)
    P      = build_payoff_matrix(grid, instruments)
    entry  = build_entry_vectors(instruments)
    greeks = build_greek_vectors(instruments, spot_price)

    return PayoffPackage(
        instruments    = instruments,
        grid           = grid,
        payoff_matrix  = P,
        entry          = entry,
        greeks         = greeks,
        spot_price     = spot_price,
        n_instruments  = len(instruments),
        n_price_points = len(grid.prices),
    )


# ─────────────────────────────────────────────────────────────────────────────
# UNIT TESTS — Iron Condor верификация
# ─────────────────────────────────────────────────────────────────────────────

def _make_test_iron_condor() -> tuple[list[OptionInstrument], npt.NDArray[np.float64]]:
    """
    Создаёт тестовый Iron Condor и ожидаемые P&L значения.

    Структура (spot = $2000, все опционы одной экспирации):
        Leg 1: SHORT PUT  $1900  bid=0.020 ETH → receive 0.020 × 2000 = $40
        Leg 2: LONG  PUT  $1800  ask=0.008 ETH → pay    0.008 × 2000 = $16
        Leg 3: SHORT CALL $2100  bid=0.018 ETH → receive 0.018 × 2000 = $36
        Leg 4: LONG  CALL $2200  ask=0.007 ETH → pay    0.007 × 2000 = $14

    Net credit = 40 - 16 + 36 - 14 = $46 USD
    Max profit  = $46 (цена между $1900 и $2100)
    Max loss PUT side  = (1900 - 1800) × 2000 - 46 = 200000 - 46... нет.

    Всё в ETH-единицах, потом × spot:
        Net credit ETH = 0.020 - 0.008 + 0.018 - 0.007 = 0.023 ETH
        Net credit USD = 0.023 × 2000 = $46

    P&L при S=1800 (пут спред в деньгах):
        put1900 payoff = max(1900-1800, 0) = 100 ETH → short = −100 ETH payoff
        put1800 payoff = max(1800-1800, 0) = 0
        call2100 payoff = 0
        call2200 payoff = 0
        payoff_eth = (-1)×100 + 1×0 + (-1)×0 + 1×0 = -100 ETH
        payoff_usd = -100 × 1800 = -$180,000  ← ЭТО НЕПРАВИЛЬНО

    Стоп — ошибка в интерпретации. Payoff матрица считает в ETH,
    но это INTRINSIC VALUE одного контракта (1 ETH номинал).
    100 ETH intrinsic при strike 1900 vs spot 1800 = это не 100 ETH
    это $100 на контракт (разница страйков в USD).

    Правильная интерпретация Deribit:
        Payoff колла = max(S - K, 0) / S  — в ETH (Deribit расчёт)
        Payoff пута  = max(K - S, 0) / S  — в ETH

    НО в нашей реализации мы считаем в USD intrinsic:
        P[j,i] = max(S - K, 0)   для колла  (USD на контракт)
        P[j,i] = max(K - S, 0)   для пута   (USD на контракт)
    И entry_cost тоже в USD (ETH × spot).
    Это самосогласованная система: всё в USD.

    Правильный расчёт для теста:
        entry_cost_usd = (ask1 × spot - bid2 × spot - bid3 × spot + ask4 × spot)
        Нет, для шортов мы ПОЛУЧАЕМ bid:
        cost = ask_long - bid_short = 0.008×2000 + 0.007×2000 - 0.020×2000 - 0.018×2000
             = 16 + 14 - 40 - 36 = -46 USD (отрицательная = получаем кредит)

    P&L при S=2000 (ATM, все legs OTM):
        payoff_usd = [max(1900-2000,0)×(-1) + max(1800-2000,0)×1
                    + max(2000-2100,0)×(-1) + max(2000-2200,0)×1] = 0
        pnl = 0 - (-46) = +$46 ✓

    P&L при S=1800 (пут спред полностью ITM):
        put1900 payoff (short): -max(1900-1800,0) = -100 USD per contract
        put1800 payoff (long):  +max(1800-1800,0) = 0
        total payoff_usd = -100
        pnl = -100 - (-46) = -$54 ✓

    P&L при S=1700 (ниже обоих страйков):
        put1900 payoff (short): -max(1900-1700,0) = -200
        put1800 payoff (long):  +max(1800-1700,0) = +100
        total payoff_usd = -100 (max loss on put spread)
        pnl = -100 - (-46) = -$54 ✓ (ограничен шириной спреда)

    P&L при S=2200 (колл спред полностью ITM):
        call2100 payoff (short): -max(2200-2100,0) = -100
        call2200 payoff (long):  +max(2200-2200,0) = 0
        total payoff_usd = -100
        pnl = -100 - (-46) = -$54 ✓

    P&L при S=2300:
        call2100 payoff (short): -max(2300-2100,0) = -200
        call2200 payoff (long):  +max(2300-2200,0) = +100
        total payoff_usd = -100
        pnl = -100 - (-46) = -$54 ✓
    """
    spot = 2_000.0

    instruments = [
        OptionInstrument(  # Leg 1: SHORT PUT 1900
            name="TEST-PUT-1900", option_type="put", strike=1_900.0,
            best_bid=0.020, best_ask=0.025, mark_price=0.022,
            delta=-0.30, gamma=0.001, theta=-0.001, vega=0.5,
            days_to_expiry=7.0, mark_iv=80.0, liquidity_score=0.8,
            open_interest=100.0, volume_usd_24h=100_000.0, spread_pct=0.05,
        ),
        OptionInstrument(  # Leg 2: LONG PUT 1800
            name="TEST-PUT-1800", option_type="put", strike=1_800.0,
            best_bid=0.006, best_ask=0.008, mark_price=0.007,
            delta=-0.15, gamma=0.0005, theta=-0.0005, vega=0.3,
            days_to_expiry=7.0, mark_iv=85.0, liquidity_score=0.7,
            open_interest=50.0, volume_usd_24h=50_000.0, spread_pct=0.10,
        ),
        OptionInstrument(  # Leg 3: SHORT CALL 2100
            name="TEST-CALL-2100", option_type="call", strike=2_100.0,
            best_bid=0.018, best_ask=0.022, mark_price=0.020,
            delta=0.28, gamma=0.001, theta=-0.001, vega=0.5,
            days_to_expiry=7.0, mark_iv=78.0, liquidity_score=0.8,
            open_interest=100.0, volume_usd_24h=100_000.0, spread_pct=0.10,
        ),
        OptionInstrument(  # Leg 4: LONG CALL 2200
            name="TEST-CALL-2200", option_type="call", strike=2_200.0,
            best_bid=0.005, best_ask=0.007, mark_price=0.006,
            delta=0.15, gamma=0.0005, theta=-0.0005, vega=0.3,
            days_to_expiry=7.0, mark_iv=82.0, liquidity_score=0.7,
            open_interest=50.0, volume_usd_24h=50_000.0, spread_pct=0.15,
        ),
    ]

    # Iron Condor: short inner, long outer
    # Leg 1: SHORT → x = -1
    # Leg 2: LONG  → x = +1
    # Leg 3: SHORT → x = -1
    # Leg 4: LONG  → x = +1
    x = np.array([-1.0, 1.0, -1.0, 1.0])

    return instruments, x, spot


def run_unit_tests(verbose: bool = False) -> None:
    """
    Запускает тесты Payoff Engine.

    Тесты:
        1. build_price_grid — количество точек, наличие стресс-точек
        2. build_payoff_matrix — колл/пут значения на конкретных ценах
        3. compute_entry_cost_usd — bid/ask split корректность
        4. pnl_vector — Iron Condor P&L на ключевых ценах
        5. build_payoff_package — целостность всего пакета

    Args:
        verbose: Печатать детальный вывод по каждому тесту.
    """
    print("Запуск unit tests Step 2: Payoff Engine\n")
    passed = 0
    failed = 0

    def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if condition:
            passed += 1
            if verbose:
                print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name}" + (f": {detail}" if detail else ""))

    EPS = 1e-6  # допуск для float сравнений

    # ── Test 1: Price Grid ────────────────────────────────────────────────────
    print("TEST 1: build_price_grid")
    grid = build_price_grid(2_000.0)

    primary_expected = len(np.arange(GRID_START, GRID_STOP, GRID_STEP))
    total_expected   = len(set(np.arange(GRID_START, GRID_STOP, GRID_STEP).tolist()) | set(STRESS_POINTS))

    check("Количество точек в сетке",
          len(grid.prices) == total_expected,
          f"ожидалось {total_expected}, получено {len(grid.prices)}")

    check("Стресс-точки включены",
          all(s in grid.prices for s in STRESS_POINTS),
          f"missing: {[s for s in STRESS_POINTS if s not in grid.prices]}")

    check("Сетка отсортирована по возрастанию",
          all(grid.prices[i] < grid.prices[i+1] for i in range(len(grid.prices)-1)))

    check("Нет дублей",
          len(grid.prices) == len(set(grid.prices.tolist())))

    check("spot_index близок к $2000",
          abs(grid.prices[grid.spot_index] - 2_000.0) <= GRID_STEP,
          f"grid.prices[spot_index]={grid.prices[grid.spot_index]}")

    if verbose:
        print(f"  Итого точек: {len(grid.prices)} ({grid.n_primary} основных + {grid.n_stress} стресс)")
        print(f"  Диапазон: ${grid.prices[0]:.0f} – ${grid.prices[-1]:.0f}")
        print(f"  Spot index: {grid.spot_index} → ${grid.prices[grid.spot_index]:.0f}")
    print()

    # ── Test 2: Payoff Matrix ─────────────────────────────────────────────────
    print("TEST 2: build_payoff_matrix — call/put intrinsic values")
    instruments, x_ic, spot = _make_test_iron_condor()
    P = build_payoff_matrix(grid, instruments)

    # Shape
    check("Shape матрицы [M, N]",
          P.shape == (len(grid.prices), len(instruments)),
          f"got {P.shape}")

    # Колл 2100: при S=2000 OTM → 0, при S=2200 ITM → 100
    # np.argwhere returns shape [K, 1] → flatten to get scalar index
    idx_2000 = int(np.argwhere(grid.prices == 2_000.0).flatten()[0])
    idx_2200 = int(np.argwhere(grid.prices == 2_200.0).flatten()[0])
    call_2100_at_2000 = P[idx_2000, 2]  # instrument index 2 = CALL 2100
    call_2100_at_2200 = P[idx_2200, 2]

    check("CALL 2100 OTM при S=2000 → payoff=0",
          abs(call_2100_at_2000) < EPS,
          f"got {call_2100_at_2000}")

    check("CALL 2100 ITM при S=2200 → payoff=100",
          abs(call_2100_at_2200 - 100.0) < EPS,
          f"got {call_2100_at_2200}")

    # Пут 1900: при S=2000 OTM → 0, при S=1800 ITM → 100
    idx_1800 = int(np.argwhere(grid.prices == 1_800.0).flatten()[0])
    put_1900_at_2000 = P[idx_2000, 0]  # instrument index 0 = PUT 1900
    put_1900_at_1800 = P[idx_1800, 0]

    check("PUT 1900 OTM при S=2000 → payoff=0",
          abs(put_1900_at_2000) < EPS,
          f"got {put_1900_at_2000}")

    check("PUT 1900 ITM при S=1800 → payoff=100",
          abs(put_1900_at_1800 - 100.0) < EPS,
          f"got {put_1900_at_1800}")

    # Payoff неотрицателен (intrinsic value ≥ 0 всегда)
    check("Все payoff ≥ 0",
          np.all(P >= 0.0),
          f"min={P.min():.4f}")

    if verbose:
        print(f"  Shape: {P.shape}")
        print(f"  CALL 2100 @ S=2000: {call_2100_at_2000:.2f}, @ S=2200: {call_2100_at_2200:.2f}")
        print(f"  PUT  1900 @ S=2000: {put_1900_at_2000:.2f}, @ S=1800: {put_1900_at_1800:.2f}")
    print()

    # ── Test 3: Entry Cost (bid/ask split) ────────────────────────────────────
    print("TEST 3: compute_entry_cost_usd — bid/ask split")
    entry = build_entry_vectors(instruments)

    # Iron Condor: x = [-1, +1, -1, +1]
    # cost = ask_long - bid_short (в ETH), × spot
    # Лонги: inst[1] (PUT1800, ask=0.008), inst[3] (CALL2200, ask=0.007)
    # Шорты: inst[0] (PUT1900, bid=0.020), inst[2] (CALL2100, bid=0.018)
    # cost_eth = (0.008 + 0.007) - (0.020 + 0.018) = 0.015 - 0.038 = -0.023
    # cost_usd = -0.023 × 2000 = -46
    expected_cost = -46.0
    actual_cost   = compute_entry_cost_usd(x_ic, entry, spot)

    check("Iron Condor net credit = -$46",
          abs(actual_cost - expected_cost) < EPS,
          f"ожидалось {expected_cost}, получено {actual_cost:.4f}")

    # Чисто лонг позиция (одна нога)
    x_long_only = np.array([0.0, 1.0, 0.0, 0.0])  # LONG PUT 1800, ask=0.008
    cost_long = compute_entry_cost_usd(x_long_only, entry, spot)
    check("Лонг-только платит ask × spot",
          abs(cost_long - 0.008 * spot) < EPS,
          f"ожидалось {0.008 * spot}, получено {cost_long}")

    # Чисто шорт позиция (одна нога)
    x_short_only = np.array([-1.0, 0.0, 0.0, 0.0])  # SHORT PUT 1900, bid=0.020
    cost_short = compute_entry_cost_usd(x_short_only, entry, spot)
    check("Шорт-только получает bid × spot (отрицательный cost)",
          abs(cost_short - (-0.020 * spot)) < EPS,
          f"ожидалось {-0.020 * spot}, получено {cost_short}")

    if verbose:
        print(f"  Iron Condor entry cost: ${actual_cost:.2f}")
        print(f"  Long PUT 1800 cost: ${cost_long:.2f}")
        print(f"  Short PUT 1900 cost: ${cost_short:.2f}")
    print()

    # ── Test 4: P&L Vector — Iron Condor ─────────────────────────────────────
    print("TEST 4: pnl_vector — Iron Condor проверка")

    pnl_vec = pnl_vector(x_ic, P, entry, grid, spot)

    idx_2000 = int(np.argwhere(grid.prices == 2_000.0).flatten()[0])
    idx_1800 = int(np.argwhere(grid.prices == 1_800.0).flatten()[0])
    idx_2200 = int(np.argwhere(grid.prices == 2_200.0).flatten()[0])

    # Ожидаемые P&L:
    # S=2000 (ATM, все legs OTM): payoff=0, pnl = 0 - (-46) = +$46
    pnl_at_2000 = pnl_vec[idx_2000]
    check("P&L при S=2000 (все OTM) = +$46",
          abs(pnl_at_2000 - 46.0) < EPS,
          f"получено {pnl_at_2000:.4f}")

    # S=1800 (пут спред ITM): payoff = -100 USD (short put1900 ITM, long put1800 ATM)
    # pnl = -100 - (-46) = -$54
    pnl_at_1800 = pnl_vec[idx_1800]
    check("P&L при S=1800 (пут спред ITM) = -$54",
          abs(pnl_at_1800 - (-54.0)) < EPS,
          f"получено {pnl_at_1800:.4f}")

    # S=1700 (ниже обоих страйков): payoff = -200 + 100 = -100 (спред ограничивает)
    idx_1700 = int(np.argwhere(grid.prices == 1_700.0).flatten()[0])
    pnl_at_1700 = pnl_vec[idx_1700]
    check("P&L при S=1700 (max loss пут стороны) = -$54",
          abs(pnl_at_1700 - (-54.0)) < EPS,
          f"получено {pnl_at_1700:.4f}")

    # S=2200 (колл спред ITM): payoff = -100 (short call2100 ITM, long call2200 ATM)
    pnl_at_2200 = pnl_vec[idx_2200]
    check("P&L при S=2200 (колл спред ITM) = -$54",
          abs(pnl_at_2200 - (-54.0)) < EPS,
          f"получено {pnl_at_2200:.4f}")

    # S=2300 (выше обоих страйков): max loss call стороны — тоже -54
    idx_2300 = int(np.argwhere(grid.prices == 2_300.0).flatten()[0])
    pnl_at_2300 = pnl_vec[idx_2300]
    check("P&L при S=2300 (max loss колл стороны) = -$54",
          abs(pnl_at_2300 - (-54.0)) < EPS,
          f"получено {pnl_at_2300:.4f}")

    # P&L между страйками должен быть максимальным (= net credit = +46)
    idx_2050 = int(np.argwhere(grid.prices == 2_050.0).flatten()[0])
    pnl_at_2050 = pnl_vec[idx_2050]
    check("P&L при S=2050 (зона прибыли) = +$46",
          abs(pnl_at_2050 - 46.0) < EPS,
          f"получено {pnl_at_2050:.4f}")

    if verbose:
        print()
        print("  Iron Condor P&L по ценовой сетке:")
        print(f"  {'Цена ETH':>12}  {'P&L (USD)':>10}")
        print(f"  {'-'*12}  {'-'*10}")
        display_prices = [1_400, 1_600, 1_700, 1_800, 1_900, 2_000,
                          2_050, 2_100, 2_200, 2_300, 2_500, 2_700]
        for dp in display_prices:
            matches = np.argwhere(grid.prices == dp).flatten()
            if len(matches) > 0:
                idx = int(matches[0])
                marker = " ← spot" if dp == 2_000 else ""
                print(f"  ${dp:>10,.0f}  ${pnl_vec[idx]:>9.2f}{marker}")
    print()

    # ── Test 5: build_payoff_package ─────────────────────────────────────────
    print("TEST 5: build_payoff_package — целостность пакета")
    pkg = build_payoff_package(instruments, spot)

    check("n_instruments корректно", pkg.n_instruments == len(instruments))
    check("n_price_points корректно", pkg.n_price_points == len(grid.prices))
    check("payoff_matrix shape корректна",
          pkg.payoff_matrix.shape == (pkg.n_price_points, pkg.n_instruments))
    check("theta_usd = theta_eth × spot",
          abs(pkg.greeks.theta_usd[0] - instruments[0].theta * spot) < EPS)
    check("margin_vec > 0 для всех",
          np.all(pkg.greeks.margin_vec > 0))

    # Пустой список → ValueError
    try:
        build_payoff_package([], spot)
        check("ValueError при пустом списке", False, "исключение не было выброшено")
    except ValueError:
        check("ValueError при пустом списке", True)

    if verbose:
        print(f"  Package: {pkg.n_instruments} инструментов, {pkg.n_price_points} ценовых точек")
        print(f"  Theta USD[0]: {pkg.greeks.theta_usd[0]:.4f} USD/день")
        print(f"  Margin[0]: ${pkg.greeks.margin_vec[0]:.2f}")
    print()

    # ── Summary ───────────────────────────────────────────────────────────────
    total = passed + failed
    print("─" * 50)
    if failed == 0:
        print(f"  ✓ Все {total} тестов прошли успешно.")
    else:
        print(f"  ✗ {failed} из {total} тестов провалились.")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ETH Options Payoff Engine — unit tests"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Показать детальный вывод по каждому тесту",
    )
    args = parser.parse_args()
    run_unit_tests(verbose=args.verbose)


if __name__ == "__main__":
    main()
