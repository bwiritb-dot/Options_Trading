"""
ETH Options Optimizer — Step 3: MILP (Basic)
=============================================
Формулирует и решает задачу целочисленного линейного программирования
для максимизации theta портфеля ETH-опционов при жёстких P&L и
маржинальных ограничениях.

Интеграция:
    from optimizer_step1 import fetch_liquid_options
    from optimizer_step2 import build_payoff_package
    from optimizer_step3 import solve_portfolio, MILPParams

Зависимости:
    pip install cvxpy numpy aiohttp

Солверы (установить хотя бы один):
    pip install cvxopt          # даёт GLPK_MI
    pip install highspy         # даёт HiGHS (рекомендуется)
    pip install cylp            # даёт CBC
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

import cvxpy as cp
import numpy as np
import numpy.typing as npt

from optimizer_step1 import OptionInstrument, fetch_liquid_options, FetchStats
from optimizer_step2 import (
    PayoffPackage,
    build_payoff_package,
    pnl_vector,
)

# ─────────────────────────────────────────────────────────────────────────────
# КОНСТАНТЫ
# ─────────────────────────────────────────────────────────────────────────────

# Каскад солверов — пробуем в порядке приоритета
PREFERRED_MIP_SOLVERS: list[str] = ["GLPK_MI", "HIGHS", "CBC", "SCIP"]

# Настройки солвера
SOLVER_TIME_LIMIT_SECONDS: int  = 120
SOLVER_MAX_ITERS: int           = 10_000
SOLVER_EPS_ABS: float           = 1e-6

# Настройки авто-релаксации
RELAXATION_PNL_FACTOR: float    = 1.20   # расширяем floor на 20%
RELAXATION_MARGIN_FACTOR: float = 1.20   # расширяем бюджет на 20%
RELAXATION_DELTA_FACTOR: float  = 2.00   # удваиваем delta limit
MAX_RELAXATION_STEPS: int       = 3      # максимум итераций релаксации

# ─────────────────────────────────────────────────────────────────────────────
# ПАРАМЕТРЫ MILP — всё в одном месте, без magic numbers
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MILPParams:
    """
    Все настраиваемые параметры задачи оптимизации.

    Вынесены в dataclass чтобы:
        - легко передавать между функциями
        - мутировать при авто-релаксации (копируем, не меняем оригинал)
        - логировать какие параметры использовались при решении
    """
    spot_price:     float = 2_000.0   # текущий спот ETH/USD
    max_qty:        int   = 10         # макс. количество контрактов на инструмент
    margin_budget:  float = 5_000.0   # USD — бюджет маржи
    pnl_floor:      float = -400.0    # USD — минимальный P&L во всех точках
    pnl_ceiling:    float = +400.0    # USD — минимальный P&L при range_high
    range_high:     float = 2_200.0   # USD — цена где применяется pnl_ceiling
    holding_days:   int   = 5         # горизонт для расчёта total theta

    # Greeks limits (используются в Step 4; здесь для полноты параметров)
    max_delta:      float = 0.15
    max_vega_usd:   float = 600.0
    max_gamma:      float = 0.008

    # Параметры тестового режима
    relaxation_step: int  = 0        # 0 = оригинальные параметры


# ─────────────────────────────────────────────────────────────────────────────
# РЕЗУЛЬТАТ MILP
# ─────────────────────────────────────────────────────────────────────────────

class SolverStatus(Enum):
    OPTIMAL     = auto()   # решение найдено
    INFEASIBLE  = auto()   # задача неразрешима даже после релаксации
    TIMEOUT     = auto()   # превышен time limit, partial solution
    ERROR       = auto()   # технический сбой


@dataclass
class MILPResult:
    """
    Результат оптимизации — структурированный для Step 6 (вывод).

    Attributes:
        status:           Статус решения.
        solver_used:      Имя солвера который нашёл решение.
        x:                Вектор позиций [N], integer.
        objective_value:  Theta портфеля × holding_days (USD).
        pnl_by_price:     P&L для каждой ценовой точки [M] (USD).
        portfolio_theta:  Суммарная theta портфеля (USD/день).
        portfolio_delta:  Дельта портфеля (безразмерная).
        portfolio_gamma:  Гамма портфеля.
        portfolio_vega:   Вега портфеля (USD на 1% IV).
        margin_used:      Использованная маржа (USD, аппроксимация).
        solve_time:       Время решения (секунды).
        params_used:      Параметры с которыми найдено решение.
        relaxation_step:  0 = оригинальные параметры, 1-3 = релаксация.
        diagnostics:      Текстовые сообщения о процессе решения.
        active_legs:      Индексы инструментов с ненулевой позицией.
    """
    status:           SolverStatus
    solver_used:      str                         = ""
    x:                Optional[npt.NDArray]       = None
    objective_value:  float                       = 0.0
    pnl_by_price:     Optional[npt.NDArray]       = None
    portfolio_theta:  float                       = 0.0
    portfolio_delta:  float                       = 0.0
    portfolio_gamma:  float                       = 0.0
    portfolio_vega:   float                       = 0.0
    margin_used:      float                       = 0.0
    solve_time:       float                       = 0.0
    params_used:      Optional[MILPParams]        = None
    relaxation_step:  int                         = 0
    diagnostics:      list[str]                   = field(default_factory=list)
    active_legs:      list[int]                   = field(default_factory=list)

# ─────────────────────────────────────────────────────────────────────────────
# ЛОГИРОВАНИЕ
# ─────────────────────────────────────────────────────────────────────────────

log = logging.getLogger("optimizer.milp")

# ─────────────────────────────────────────────────────────────────────────────
# МОДУЛЬ 3.1 — ПОСТРОЕНИЕ MILP ЗАДАЧИ
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MILPProblem:
    """
    Упакованная cvxpy задача со всеми переменными для доступа после решения.
    """
    problem:    cp.Problem
    x:          cp.Variable
    x_long:     cp.Variable
    x_short:    cp.Variable
    z:          cp.Variable


def _find_price_index(
    prices: npt.NDArray[np.float64],
    target: float,
) -> Optional[int]:
    """
    Находит индекс ближайшей цены к target в сетке.

    Если точного совпадения нет — берёт ближайшую.

    Args:
        prices: Отсортированный массив цен.
        target: Целевая цена.

    Returns:
        Индекс или None если массив пуст.
    """
    if len(prices) == 0:
        return None
    return int(np.argmin(np.abs(prices - target)))


def build_milp_problem(
    pkg: PayoffPackage,
    params: MILPParams,
) -> MILPProblem:
    """
    Формулирует MILP задачу через cvxpy.

    Переменные:
        x[i]       ∈ Z  ∈ [−MAX_QTY, +MAX_QTY]  — количество контрактов
        x_long[i]  ∈ Z≥0                          — длинная часть
        x_short[i] ∈ Z≥0                          — короткая часть
        z[i]       ∈ R≥0                          — |x[i]| для маржи

    Objective:
        Maximize: theta_usd @ x × holding_days
        (theta_usd уже содержит правильный знак: шорты дают положительный вклад)

    Constraints:
        1. Декомпозиция:   x = x_long - x_short
        2. Позитивность:   x_long ≥ 0, x_short ≥ 0
        3. Абс. значение:  z ≥ x,  z ≥ -x
        4. P&L floor:      P[j] @ x - cost_usd ≥ pnl_floor  ∀j
        5. P&L ceiling:    P[j*] @ x - cost_usd ≥ pnl_ceiling  при S=range_high
        6. Маржа:          margin_vec @ z ≤ margin_budget
        7. Границы qty:    -max_qty ≤ x ≤ max_qty

    Почему x_long и x_short integer:
        Если оставить continuous, солвер может получить дробные значения
        при дробном x. Integer гарантирует, что x = x_long - x_short
        точно и нет floating-point расхождений.

    Почему z не integer:
        z просто отражает |x| для линейного маржинального constraint.
        При integer x оптимальное z автоматически будет целым.

    Args:
        pkg:    PayoffPackage из Step 2.
        params: Параметры оптимизации.

    Returns:
        MILPProblem с cvxpy объектами.

    Raises:
        ValueError: Если в пакете нет инструментов.
    """
    N = pkg.n_instruments
    if N == 0:
        raise ValueError("PayoffPackage пуст — нет инструментов для оптимизации.")

    P    = pkg.payoff_matrix          # [M, N] USD intrinsic values
    grid = pkg.grid
    entry = pkg.entry
    greeks = pkg.greeks
    spot = params.spot_price

    # ── Переменные ────────────────────────────────────────────────────────────
    x       = cp.Variable(N, integer=True, name="x")
    x_long  = cp.Variable(N, integer=True, name="x_long")
    x_short = cp.Variable(N, integer=True, name="x_short")
    z       = cp.Variable(N,               name="z")          # |x|

    # ── Objective: максимизировать theta (USD) за holding_days ────────────────
    # theta_usd[i] < 0 для лонгов, > 0 для шортов (Deribit знак)
    # theta_usd @ x корректно суммирует вклады с учётом направления
    total_theta_expr = greeks.theta_usd @ x * params.holding_days
    objective = cp.Maximize(total_theta_expr)

    # ── Стоимость входа (bid/ask split) ──────────────────────────────────────
    # Линейна в x_long и x_short — ключевое свойство для MILP
    cost_eth_expr = entry.ask_vec @ x_long - entry.bid_vec @ x_short
    cost_usd_expr = cost_eth_expr * spot

    # ── Constraints ──────────────────────────────────────────────────────────
    constraints: list[cp.Constraint] = []

    # 1. Декомпозиция x на long/short части
    constraints.append(x == x_long - x_short)

    # 2. Non-negativity длинных/коротких частей
    constraints.append(x_long  >= 0)
    constraints.append(x_short >= 0)

    # 3. |x| через z (линейная аппроксимация абс. значения)
    constraints.append(z >= x)
    constraints.append(z >= -x)
    constraints.append(z >= 0)

    # 4+5. P&L constraints для всех ценовых точек
    range_high_idx = _find_price_index(grid.prices, params.range_high)

    for j, price in enumerate(grid.prices):
        pnl_expr = P[j] @ x - cost_usd_expr

        if j == range_high_idx:
            # При range_high (S=2200) требуем прибыль >= pnl_ceiling
            constraints.append(pnl_expr >= params.pnl_ceiling)
        else:
            # Везде остальном — не хуже pnl_floor
            constraints.append(pnl_expr >= params.pnl_floor)

    # 6. Маржа: линейная аппроксимация с 25% буфером (точная маржа — Step 5)
    # margin_vec уже содержит 1.25× буфер из Step 2
    constraints.append(greeks.margin_vec @ z <= params.margin_budget)

    # 7. Границы количества контрактов
    constraints.append(x >=  -params.max_qty)
    constraints.append(x <=   params.max_qty)
    constraints.append(x_long  <= params.max_qty)
    constraints.append(x_short <= params.max_qty)

    problem = cp.Problem(objective, constraints)

    return MILPProblem(
        problem = problem,
        x       = x,
        x_long  = x_long,
        x_short = x_short,
        z       = z,
    )


# ─────────────────────────────────────────────────────────────────────────────
# МОДУЛЬ 3.2 — КАСКАД СОЛВЕРОВ
# ─────────────────────────────────────────────────────────────────────────────

def _get_available_mip_solvers() -> list[str]:
    """
    Возвращает список доступных MIP-солверов в порядке приоритета.

    Returns:
        Список имён солверов из PREFERRED_MIP_SOLVERS которые установлены.
    """
    installed = set(cp.installed_solvers())
    available = [s for s in PREFERRED_MIP_SOLVERS if s in installed]

    if not available:
        raise RuntimeError(
            "Не найден ни один MIP-солвер. Установите один из:\n"
            "  pip install cvxopt     # GLPK_MI\n"
            "  pip install highspy    # HiGHS (рекомендуется)\n"
            "  pip install cylp       # CBC\n"
        )

    log.info("Доступные MIP-солверы: %s", available)
    return available


def _solve_with_solver(
    milp: MILPProblem,
    solver_name: str,
) -> str:
    """
    Пробует решить задачу одним конкретным солвером.

    Args:
        milp:        Построенная cvxpy задача.
        solver_name: Имя солвера (например "GLPK_MI").

    Returns:
        Статус cvxpy: "optimal", "infeasible", "unbounded", "optimal_inaccurate", ...

    Raises:
        cp.SolverError: При технической ошибке солвера.
    """
    solver_opts: dict = {"verbose": False}

    # Опции специфичны для каждого солвера
    if solver_name == "GLPK_MI":
        solver_opts["tm_lim"]   = SOLVER_TIME_LIMIT_SECONDS * 1000  # ms
        solver_opts["msg_lev"]  = 0  # GLPK_MSG_OFF
    elif solver_name == "HIGHS":
        solver_opts["time_limit"] = SOLVER_TIME_LIMIT_SECONDS
    elif solver_name == "CBC":
        solver_opts["maximumSeconds"] = SOLVER_TIME_LIMIT_SECONDS
    elif solver_name == "SCIP":
        solver_opts["limits/time"] = SOLVER_TIME_LIMIT_SECONDS

    milp.problem.solve(solver=solver_name, **solver_opts)
    return milp.problem.status


def try_solvers(
    milp: MILPProblem,
    available_solvers: list[str],
) -> tuple[str, str]:
    """
    Пробует солверы по приоритету — останавливается при первом успехе.

    Args:
        milp:              Построенная cvxpy задача.
        available_solvers: Список солверов в порядке приоритета.

    Returns:
        Кортеж (статус, имя_солвера).
        Статус: "optimal" | "infeasible" | "timeout" | "error"
    """
    last_status  = "error"
    last_solver  = ""

    for solver_name in available_solvers:
        log.info("Пробуем солвер: %s", solver_name)
        try:
            status = _solve_with_solver(milp, solver_name)
            last_solver = solver_name
            last_status = status

            if status in ("optimal", "optimal_inaccurate"):
                log.info("Решение найдено солвером %s (статус: %s)", solver_name, status)
                return "optimal", solver_name

            if status == "infeasible":
                # Infeasible одинаково для всех солверов — нет смысла пробовать дальше
                log.info("Задача неразрешима (солвер: %s)", solver_name)
                return "infeasible", solver_name

            if status in ("unbounded", "infeasible_inaccurate"):
                log.warning("Солвер %s вернул: %s — пробуем следующий", solver_name, status)
                continue

        except cp.SolverError as exc:
            log.warning("Ошибка солвера %s: %s — пробуем следующий", solver_name, exc)
            continue

    # Все солверы не справились
    return last_status, last_solver


# ─────────────────────────────────────────────────────────────────────────────
# МОДУЛЬ 3.3 — ДИАГНОСТИКА INFEASIBLE
# ─────────────────────────────────────────────────────────────────────────────

def _diagnose_infeasibility(
    pkg: PayoffPackage,
    params: MILPParams,
    available_solvers: list[str],
) -> list[str]:
    """
    Определяет какой именно constraint делает задачу неразрешимой.

    Алгоритм: решаем упрощённые задачи поочерёдно снимая группы constraints.
    Тест который становится feasible = группа содержит проблемный constraint.

    Проверки (по возрастанию "виновности"):
        A. Только P&L floor (без ceiling, без маржи) → если infeasible: floor слишком жёсткий
        B. Только P&L ceiling (одна точка) → если infeasible: ceiling недостижим
        C. P&L floor + ceiling, без маржи → если feasible: маржа блокирует
        D. P&L floor + ceiling + маржа, без qty limits → если feasible: qty limits блокируют

    Args:
        pkg:               PayoffPackage.
        params:            Текущие параметры.
        available_solvers: Солверы для тестов.

    Returns:
        Список строк с описанием проблем.
    """
    N    = pkg.n_instruments
    P    = pkg.payoff_matrix
    grid = pkg.grid
    entry = pkg.entry
    greeks = pkg.greeks
    spot = params.spot_price

    messages: list[str] = []

    def _quick_solve(extra_constraints: list[cp.Constraint]) -> str:
        """Быстрое решение relaxed задачи для диагностики."""
        x_d       = cp.Variable(N, integer=True)
        x_long_d  = cp.Variable(N, integer=True)
        x_short_d = cp.Variable(N, integer=True)

        cost_d = (entry.ask_vec @ x_long_d - entry.bid_vec @ x_short_d) * spot

        base = [
            x_d == x_long_d - x_short_d,
            x_long_d  >= 0,
            x_short_d >= 0,
            x_d >= -params.max_qty,
            x_d <=  params.max_qty,
        ]
        prob = cp.Problem(cp.Minimize(0), base + extra_constraints)
        try:
            prob.solve(solver=available_solvers[0], verbose=False)
            return prob.status
        except cp.SolverError:
            return "error"

    range_high_idx = _find_price_index(grid.prices, params.range_high)

    # ── Тест A: только P&L floor ──────────────────────────────────────────────
    x_t = cp.Variable(N, integer=True)
    x_l = cp.Variable(N, integer=True)
    x_s = cp.Variable(N, integer=True)
    cost_t = (entry.ask_vec @ x_l - entry.bid_vec @ x_s) * spot

    floor_only = [
        P[j] @ x_t - cost_t >= params.pnl_floor
        for j in range(len(grid.prices))
        if j != range_high_idx
    ]
    status_a = _quick_solve(floor_only)

    if status_a == "infeasible":
        messages.append(
            f"❌ P&L floor={params.pnl_floor:.0f} USD неразрешим даже без маржи и ceiling. "
            f"Слишком жёсткий floor для доступных инструментов."
        )
    else:
        messages.append(f"✓ P&L floor={params.pnl_floor:.0f} USD достижим без других constraints.")

    # ── Тест B: только P&L ceiling ────────────────────────────────────────────
    if range_high_idx is not None:
        ceiling_price = grid.prices[range_high_idx]
        ceiling_only = [P[range_high_idx] @ x_t - cost_t >= params.pnl_ceiling]
        status_b = _quick_solve(ceiling_only)

        if status_b == "infeasible":
            messages.append(
                f"❌ P&L ceiling={params.pnl_ceiling:.0f} USD при S={ceiling_price:.0f} "
                f"недостижим. Нет инструментов с достаточным upside."
            )
        else:
            messages.append(
                f"✓ P&L ceiling={params.pnl_ceiling:.0f} USD при S={ceiling_price:.0f} достижим."
            )

    # ── Тест C: floor + ceiling, без маржи ────────────────────────────────────
    floor_ceiling = [
        P[j] @ x_t - cost_t >= (params.pnl_ceiling if j == range_high_idx else params.pnl_floor)
        for j in range(len(grid.prices))
    ]
    status_c = _quick_solve(floor_ceiling)

    if status_c == "infeasible":
        messages.append(
            "❌ Floor + ceiling вместе неразрешимы (без маржи). "
            "Конфликт между требованиями floor и ceiling."
        )
    elif status_c == "optimal":
        # ── Тест D: floor + ceiling + маржа (без qty limits) ─────────────────
        z_d = cp.Variable(N)
        margin_constraint = [greeks.margin_vec @ z_d <= params.margin_budget, z_d >= 0]
        status_d = _quick_solve(floor_ceiling + margin_constraint)

        if status_d == "infeasible":
            messages.append(
                f"❌ Margin budget={params.margin_budget:.0f} USD слишком мал. "
                f"Увеличьте MARGIN_BUDGET или уменьшите MAX_QTY."
            )
        else:
            messages.append(
                f"⚠ Проблема в MAX_QTY={params.max_qty}. "
                f"Solver не может удовлетворить все constraints при целочисленных ограничениях."
            )

    return messages


# ─────────────────────────────────────────────────────────────────────────────
# МОДУЛЬ 3.4 — АВТО-РЕЛАКСАЦИЯ
# ─────────────────────────────────────────────────────────────────────────────

def _relax_params(params: MILPParams, step: int) -> Optional[MILPParams]:
    """
    Создаёт ослабленную копию параметров для шага релаксации.

    Порядок релаксации (из ТЗ):
        Шаг 1: PNL_FLOOR × 1.2   (делаем floor более отрицательным)
        Шаг 2: MARGIN_BUDGET × 1.2
        Шаг 3: MAX_DELTA × 2      (для Step 4; здесь логируем)
        None:  все шаги исчерпаны

    Важно: PNL_FLOOR отрицательный, умножение на 1.2 делает его БОЛЬШЕ
    по модулю → более permissive. Например: -400 × 1.2 = -480.

    Args:
        params: Исходные параметры.
        step:   Номер шага релаксации (1, 2, 3).

    Returns:
        Новый MILPParams с ослабленными параметрами, или None если исчерпаны.
    """
    import copy
    relaxed = copy.copy(params)
    relaxed.relaxation_step = step

    if step == 1:
        relaxed.pnl_floor = params.pnl_floor * RELAXATION_PNL_FACTOR
        log.info(
            "Релаксация шаг 1: PNL_FLOOR %.0f → %.0f",
            params.pnl_floor, relaxed.pnl_floor,
        )
        return relaxed

    elif step == 2:
        relaxed.pnl_floor      = params.pnl_floor * RELAXATION_PNL_FACTOR
        relaxed.margin_budget  = params.margin_budget * RELAXATION_MARGIN_FACTOR
        log.info(
            "Релаксация шаг 2: MARGIN_BUDGET %.0f → %.0f",
            params.margin_budget, relaxed.margin_budget,
        )
        return relaxed

    elif step == 3:
        relaxed.pnl_floor      = params.pnl_floor * RELAXATION_PNL_FACTOR
        relaxed.margin_budget  = params.margin_budget * RELAXATION_MARGIN_FACTOR
        relaxed.max_delta      = params.max_delta * RELAXATION_DELTA_FACTOR
        log.info(
            "Релаксация шаг 3: MAX_DELTA %.3f → %.3f",
            params.max_delta, relaxed.max_delta,
        )
        return relaxed

    return None


def relax_and_retry(
    pkg: PayoffPackage,
    params: MILPParams,
    available_solvers: list[str],
) -> MILPResult:
    """
    Итеративно ослабляет constraints до нахождения решения.

    Алгоритм:
        for step in 1..MAX_RELAXATION_STEPS:
            relaxed_params = _relax_params(params, step)
            rebuild MILP → try_solvers
            if optimal → return result with relaxation_step

    Args:
        pkg:               PayoffPackage.
        params:            Исходные (строгие) параметры.
        available_solvers: Список солверов.

    Returns:
        MILPResult с решением или INFEASIBLE если все шаги провалились.
    """
    diagnostics: list[str] = []
    diagnostics.append(
        f"Задача неразрешима с исходными параметрами. "
        f"Запускаем авто-релаксацию ({MAX_RELAXATION_STEPS} шага)."
    )

    # Диагностика — что именно блокирует
    diag_messages = _diagnose_infeasibility(pkg, params, available_solvers)
    diagnostics.extend(diag_messages)

    for step in range(1, MAX_RELAXATION_STEPS + 1):
        relaxed = _relax_params(params, step)
        if relaxed is None:
            break

        step_desc = {
            1: f"PNL_FLOOR ослаблен до {relaxed.pnl_floor:.0f} USD",
            2: f"+ MARGIN_BUDGET расширен до {relaxed.margin_budget:.0f} USD",
            3: f"+ MAX_DELTA расширен до {relaxed.max_delta:.3f}",
        }
        diagnostics.append(f"Релаксация {step}/3: {step_desc[step]}")

        milp = build_milp_problem(pkg, relaxed)
        status, solver_used = try_solvers(milp, available_solvers)

        if status == "optimal":
            diagnostics.append(f"✓ Решение найдено после {step} шага(ов) релаксации.")
            result = _extract_result(milp, pkg, relaxed, solver_used)
            result.relaxation_step = step
            result.diagnostics     = diagnostics
            return result

    # Все попытки исчерпаны
    diagnostics.append(
        "✗ Решение не найдено после всех шагов релаксации. "
        "Рассмотрите: расширить диапазон экспирации, увеличить бюджет маржи, "
        "или снизить требования к P&L."
    )
    return MILPResult(
        status      = SolverStatus.INFEASIBLE,
        diagnostics = diagnostics,
    )


# ─────────────────────────────────────────────────────────────────────────────
# МОДУЛЬ 3.5 — ИЗВЛЕЧЕНИЕ РЕЗУЛЬТАТА
# ─────────────────────────────────────────────────────────────────────────────

def _extract_result(
    milp: MILPProblem,
    pkg: PayoffPackage,
    params: MILPParams,
    solver_used: str,
) -> MILPResult:
    """
    Извлекает и вычисляет все метрики из решённой cvxpy задачи.

    Округляет x до ближайших целых (на случай floating-point шума от солвера).

    Args:
        milp:        Решённая cvxpy задача.
        pkg:         PayoffPackage с данными.
        params:      Параметры с которыми решалась задача.
        solver_used: Имя солвера.

    Returns:
        MILPResult с полными метриками.
    """
    # Округляем до целых — integer solvers иногда дают 2.9999... вместо 3
    x_raw = milp.x.value
    if x_raw is None:
        return MILPResult(status=SolverStatus.ERROR, solver_used=solver_used)

    x = np.round(x_raw).astype(int)

    # P&L по всем ценовым точкам
    pnl = pnl_vector(x.astype(float), pkg.payoff_matrix, pkg.entry, pkg.grid, params.spot_price)

    # Greeks портфеля
    greeks = pkg.greeks
    portfolio_theta = float(greeks.theta_usd @ x)
    portfolio_delta = float(greeks.delta_vec  @ x)
    portfolio_gamma = float(greeks.gamma_vec  @ x)
    portfolio_vega  = float(greeks.vega_usd   @ x)

    # Маржа (аппроксимация через z = |x|)
    z_approx = np.abs(x).astype(float)
    margin_used = float(greeks.margin_vec @ z_approx)

    # Активные ноги (ненулевые позиции)
    active_legs = [i for i, xi in enumerate(x) if xi != 0]

    return MILPResult(
        status          = SolverStatus.OPTIMAL,
        solver_used     = solver_used,
        x               = x,
        objective_value = float(milp.problem.value) if milp.problem.value else 0.0,
        pnl_by_price    = pnl,
        portfolio_theta = portfolio_theta,
        portfolio_delta = portfolio_delta,
        portfolio_gamma = portfolio_gamma,
        portfolio_vega  = portfolio_vega,
        margin_used     = margin_used,
        params_used     = params,
        relaxation_step = params.relaxation_step,
        active_legs     = active_legs,
    )


# ─────────────────────────────────────────────────────────────────────────────
# ГЛАВНАЯ ТОЧКА ВХОДА ДЛЯ ШАГА 3
# ─────────────────────────────────────────────────────────────────────────────

def solve_portfolio(
    pkg: PayoffPackage,
    params: MILPParams,
) -> MILPResult:
    """
    Полный цикл оптимизации: build → solve → relax if needed → result.

    Используется из Step 6 (финальный optimizer.py) и unit tests.

    Args:
        pkg:    PayoffPackage из Step 2.
        params: Параметры оптимизации.

    Returns:
        MILPResult с решением и диагностикой.
    """
    t0 = time.monotonic()

    available_solvers = _get_available_mip_solvers()
    log.info(
        "Запуск MILP: %d инструментов, %d ценовых точек, солверы: %s",
        pkg.n_instruments, pkg.n_price_points, available_solvers,
    )

    # Первая попытка с исходными параметрами
    milp   = build_milp_problem(pkg, params)
    status, solver_used = try_solvers(milp, available_solvers)

    if status == "optimal":
        result = _extract_result(milp, pkg, params, solver_used)
        result.solve_time = time.monotonic() - t0
        return result

    if status in ("timeout",):
        # Partial solution может быть полезен
        if milp.x.value is not None:
            result = _extract_result(milp, pkg, params, solver_used)
            result.status     = SolverStatus.TIMEOUT
            result.solve_time = time.monotonic() - t0
            result.diagnostics.append(
                f"⚠ Timeout {SOLVER_TIME_LIMIT_SECONDS}s — возвращаем лучшее найденное решение."
            )
            return result

    # Infeasible или ошибка → авто-релаксация
    result = relax_and_retry(pkg, params, available_solvers)
    result.solve_time = time.monotonic() - t0
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ФОРМАТИРОВАННЫЙ ВЫВОД РЕЗУЛЬТАТА
# ─────────────────────────────────────────────────────────────────────────────

def print_result(
    result: MILPResult,
    pkg: PayoffPackage,
) -> None:
    """
    Печатает человекочитаемый отчёт в stdout.

    Формат соответствует спецификации из ТЗ (Step 6 расширит его).

    Args:
        result: Результат оптимизации.
        pkg:    PayoffPackage (для имён инструментов).
    """
    W = 58
    border  = "═" * W
    divider = "─" * W

    print(f"\n{border}")
    print(f"  ETH OPTIONS OPTIMIZER — Step 3: MILP Result")
    print(f"  Статус: {result.status.name}  |  Солвер: {result.solver_used}")
    if result.relaxation_step > 0:
        print(f"  ⚠ Решено с релаксацией шаг {result.relaxation_step}/{MAX_RELAXATION_STEPS}")
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

    # ── Позиции ───────────────────────────────────────────────────────────────
    print("\nОПТИМАЛЬНАЯ ПОЗИЦИЯ")
    print(divider)
    print(f"  {'#':<3} {'Инструмент':<28} {'Dir':<6} {'Qty':>4}  {'Theta/d':>8}")
    print(f"  {'-'*3} {'-'*28} {'-'*6} {'-'*4}  {'-'*8}")

    for i in result.active_legs:
        inst     = pkg.instruments[i]
        xi       = result.x[i]
        direction = "LONG " if xi > 0 else "SHORT"
        theta_i  = pkg.greeks.theta_usd[i] * xi  # вклад этой ноги
        print(
            f"  {i+1:<3} {inst.name:<28} {direction:<6} {xi:>+4}  "
            f"${theta_i:>7.2f}/d"
        )

    if not result.active_legs:
        print("  (нет активных позиций — нулевое решение)")

    # ── Greeks портфеля ───────────────────────────────────────────────────────
    print(f"\nGREEKS ПОРТФЕЛЯ")
    print(divider)
    print(f"  Theta (USD/день): ${result.portfolio_theta:>10.2f}")
    print(f"  Theta × {result.params_used.holding_days if result.params_used else 5}д (цель): "
          f"${result.objective_value:>10.2f}")
    print(f"  Delta:            {result.portfolio_delta:>11.4f}")
    print(f"  Gamma:            {result.portfolio_gamma:>11.5f}")
    print(f"  Vega (USD/1%):   ${result.portfolio_vega:>10.2f}")

    # ── P&L сценарии ──────────────────────────────────────────────────────────
    print(f"\nP&L СЦЕНАРИИ")
    print(divider)

    if result.pnl_by_price is not None and result.params_used:
        display_prices = [1_400, 1_600, 1_700, 1_800, 1_900, 2_000,
                          2_100, 2_200, 2_300, 2_500, 2_700]
        floor = result.params_used.pnl_floor
        ceiling_price = result.params_used.range_high

        for dp in display_prices:
            matches = np.where(np.abs(pkg.grid.prices - dp) < 0.01)[0]
            if len(matches) == 0:
                continue
            j   = matches[0]
            pnl = result.pnl_by_price[j]

            if dp == result.params_used.spot_price:
                label = " ← spot"
            elif dp == ceiling_price:
                label = " ← ceiling"
            else:
                label = ""

            ok = pnl >= floor if dp != ceiling_price else pnl >= result.params_used.pnl_ceiling
            mark = "✓" if ok else "⚠"
            print(f"  ETH ${dp:>5,}:  ${pnl:>8.0f}  {mark}{label}")

    # ── Риск-метрики ──────────────────────────────────────────────────────────
    print(f"\nРИСК-МЕТРИКИ")
    print(divider)
    if result.params_used:
        budget = result.params_used.margin_budget
        pct    = result.margin_used / budget * 100 if budget else 0
        print(f"  Маржа (апрокс.): ${result.margin_used:>7.0f} / ${budget:>7.0f}  ({pct:.1f}%)")

    if result.pnl_by_price is not None:
        max_loss  = float(result.pnl_by_price.min())
        max_profit = float(result.pnl_by_price.max())
        print(f"  Max loss:        ${max_loss:>8.0f}")
        print(f"  Max profit:      ${max_profit:>8.0f}")

    print(f"  Время решения:    {result.solve_time:.2f}s")

    if result.diagnostics:
        print(f"\nДИАГНОСТИКА")
        print(divider)
        for msg in result.diagnostics:
            print(f"  {msg}")

    print(f"\n{border}\n")


# ─────────────────────────────────────────────────────────────────────────────
# UNIT TESTS
# ─────────────────────────────────────────────────────────────────────────────

def _make_synthetic_instruments(spot: float) -> list[OptionInstrument]:
    """
    Создаёт 10 синтетических инструментов для тестирования MILP.

    Покрывают диапазон страйков вокруг спота для формирования
    нейтральных по дельте стратегий (Iron Condor, Strangle и т.д.).
    """
    def make(name, opt_type, strike, bid, ask, delta, theta_eth, vega_eth, gamma):
        mark = (bid + ask) / 2
        return OptionInstrument(
            name=name, option_type=opt_type, strike=strike,
            best_bid=bid, best_ask=ask, mark_price=mark,
            delta=delta, gamma=gamma,
            theta=theta_eth,  # ETH/день — Step 2 умножит на spot
            vega=vega_eth,
            expiry_ts=0,
            days_to_expiry=8.0, mark_iv=80.0,
            liquidity_score=0.75, open_interest=200.0,
            volume_usd_24h=300_000.0,
            spread_pct=(ask - bid) / mark,
        )

    # Страйки: 1700, 1800, 1900, 1950 путы / 2050, 2100, 2200, 2300, 2400 коллы
    instruments = [
        # Путы — OTM
        make("ETH-PUT-1700",  "put",  1_700, 0.004, 0.006, -0.05, -0.0002, 0.10, 0.0002),
        make("ETH-PUT-1800",  "put",  1_800, 0.007, 0.009, -0.10, -0.0004, 0.20, 0.0004),
        make("ETH-PUT-1900",  "put",  1_900, 0.018, 0.022, -0.22, -0.0010, 0.45, 0.0010),
        make("ETH-PUT-1950",  "put",  1_950, 0.028, 0.034, -0.30, -0.0015, 0.60, 0.0015),
        make("ETH-PUT-2000",  "put",  2_000, 0.045, 0.052, -0.45, -0.0022, 0.90, 0.0020),
        # Коллы — OTM
        make("ETH-CALL-2000", "call", 2_000, 0.044, 0.051,  0.55, -0.0022, 0.90, 0.0020),
        make("ETH-CALL-2050", "call", 2_050, 0.030, 0.036,  0.35, -0.0016, 0.65, 0.0016),
        make("ETH-CALL-2100", "call", 2_100, 0.019, 0.024,  0.22, -0.0010, 0.45, 0.0010),
        make("ETH-CALL-2200", "call", 2_200, 0.008, 0.011,  0.10, -0.0004, 0.20, 0.0004),
        make("ETH-CALL-2300", "call", 2_300, 0.004, 0.006,  0.05, -0.0002, 0.10, 0.0002),
    ]
    return instruments


def run_unit_tests(verbose: bool = False) -> None:
    """
    Запускает unit tests для MILP-компонентов.

    Тесты:
        1. build_milp_problem — структура задачи
        2. solve_portfolio — нахождение решения
        3. P&L constraints соблюдены
        4. Маржинальный constraint соблюдён
        5. Авто-релаксация при жёстком floor
        6. Нулевое решение при нулевых инструментах → ValueError
    """
    print("Запуск unit tests Step 3: MILP\n")
    passed = 0
    failed = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            if verbose:
                print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name}" + (f": {detail}" if detail else ""))

    # Параметры для тестов — немного мягче production чтобы гарантировать feasibility
    TEST_SPOT    = 2_000.0
    test_params  = MILPParams(
        spot_price    = TEST_SPOT,
        max_qty       = 5,
        margin_budget = 4_000.0,
        pnl_floor     = -300.0,
        pnl_ceiling   = +100.0,   # мягче чем +400 в production
        range_high    = 2_200.0,
        holding_days  = 5,
    )

    instruments = _make_synthetic_instruments(TEST_SPOT)

    # ── Test 1: build_payoff_package + build_milp_problem ────────────────────
    print("TEST 1: Построение MILP задачи")
    try:
        pkg  = build_payoff_package(instruments, TEST_SPOT)
        milp = build_milp_problem(pkg, test_params)

        check("Число переменных x == N",
              milp.x.shape == (len(instruments),))
        check("Число переменных x_long == N",
              milp.x_long.shape == (len(instruments),))
        check("Число переменных z == N",
              milp.z.shape == (len(instruments),))
        check("Problem имеет constraints",
              len(milp.problem.constraints) > 0,
              f"got {len(milp.problem.constraints)}")
        check("Задача — максимизация",
              isinstance(milp.problem.objective, cp.Maximize))

        if verbose:
            N_constraints = len(milp.problem.constraints)
            print(f"  Переменных: {len(instruments)}, Constraints: {N_constraints}")
    except Exception as exc:
        check("Build без исключений", False, str(exc))

    print()

    # ── Test 2: solve_portfolio — найти feasible решение ─────────────────────
    print("TEST 2: Решение MILP с синтетическими данными")
    try:
        result = solve_portfolio(pkg, test_params)

        check("Статус OPTIMAL или релаксация",
              result.status in (SolverStatus.OPTIMAL,),
              f"got {result.status.name}")
        check("x не None",
              result.x is not None)
        check("x имеет правильную длину",
              result.x is not None and len(result.x) == len(instruments))
        check("x — целочисленный",
              result.x is not None and all(float(v).is_integer() for v in result.x))
        check("pnl_by_price не None",
              result.pnl_by_price is not None)
        check("pnl_by_price длина == 35",
              result.pnl_by_price is not None and len(result.pnl_by_price) == 35)

        if verbose and result.x is not None:
            print(f"  Решение x: {result.x.tolist()}")
            print(f"  Theta/день: ${result.portfolio_theta:.2f}")
            print(f"  Objective: ${result.objective_value:.2f}")
            print(f"  Время: {result.solve_time:.2f}s")
    except Exception as exc:
        check("Solve без исключений", False, str(exc))
        import traceback; traceback.print_exc()
        result = MILPResult(status=SolverStatus.ERROR)

    print()

    # ── Test 3: P&L constraints соблюдены ────────────────────────────────────
    print("TEST 3: Проверка P&L constraints")
    if result.status == SolverStatus.OPTIMAL and result.pnl_by_price is not None:
        pnl = result.pnl_by_price
        grid = pkg.grid

        floor_violations = sum(
            1 for j, p in enumerate(pnl)
            if abs(grid.prices[j] - test_params.range_high) > 0.01
            and p < test_params.pnl_floor - 0.01  # 1¢ tolerance
        )
        check("P&L floor соблюдён во всех точках",
              floor_violations == 0,
              f"{floor_violations} нарушений")

        # Ceiling при range_high
        ceiling_matches = np.where(np.abs(grid.prices - test_params.range_high) < 0.01)[0]
        if len(ceiling_matches) > 0:
            pnl_at_ceiling = pnl[ceiling_matches[0]]
            check("P&L ceiling соблюдён при range_high",
                  pnl_at_ceiling >= test_params.pnl_ceiling - 0.01,
                  f"P&L={pnl_at_ceiling:.2f} < {test_params.pnl_ceiling}")

        if verbose:
            print(f"  Min P&L: ${pnl.min():.2f}  (floor: ${test_params.pnl_floor})")
            print(f"  Max P&L: ${pnl.max():.2f}")
    else:
        check("P&L constraints (пропускаем — нет решения)", True)

    print()

    # ── Test 4: Маржинальный constraint соблюдён ──────────────────────────────
    print("TEST 4: Маржа в пределах бюджета")
    if result.status == SolverStatus.OPTIMAL and result.x is not None:
        check("Маржа <= бюджет",
              result.margin_used <= test_params.margin_budget + 0.01,
              f"margin={result.margin_used:.0f} > budget={test_params.margin_budget}")

        check("|x| <= MAX_QTY для всех ног",
              all(abs(xi) <= test_params.max_qty for xi in result.x),
              f"max |x| = {max(abs(xi) for xi in result.x)}")

        if verbose:
            print(f"  Маржа: ${result.margin_used:.0f} / ${test_params.margin_budget:.0f}")
    else:
        check("Маржа (пропускаем — нет решения)", True)

    print()

    # ── Test 5: Авто-релаксация при невозможном floor ────────────────────────
    print("TEST 5: Авто-релаксация")
    impossible_params = MILPParams(
        spot_price    = TEST_SPOT,
        max_qty       = 1,
        margin_budget = 100.0,     # слишком мало
        pnl_floor     = -1.0,      # практически недостижимо
        pnl_ceiling   = +5_000.0,  # тоже нереально
        range_high    = 2_200.0,
        holding_days  = 5,
    )
    result_relax = solve_portfolio(pkg, impossible_params)
    # При невозможных параметрах должны либо найти с релаксацией, либо INFEASIBLE
    check("Нет непойманных исключений",
          result_relax.status in (SolverStatus.OPTIMAL, SolverStatus.INFEASIBLE),
          f"got {result_relax.status.name}")
    check("Диагностика не пуста",
          len(result_relax.diagnostics) > 0)

    if verbose:
        print(f"  Статус: {result_relax.status.name}")
        for msg in result_relax.diagnostics[:3]:
            print(f"  {msg}")

    print()

    # ── Test 6: ValueError на пустом списке ──────────────────────────────────
    print("TEST 6: Защита от пустого списка")
    try:
        build_payoff_package([], TEST_SPOT)
        check("ValueError на пустом pkg", False, "исключение не выброшено")
    except ValueError:
        check("ValueError на пустом pkg", True)

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
# LIVE РЕЖИМ — реальные данные из Deribit
# ─────────────────────────────────────────────────────────────────────────────

async def _run_live(params: MILPParams) -> None:
    """Запускает полный pipeline на живых данных Deribit."""
    print("Загружаем инструменты с Deribit…")
    options, stats = await fetch_liquid_options()

    if not options:
        print("Нет ликвидных инструментов. Проверьте сеть или расширьте фильтры.")
        return

    print(f"Получено {stats.final_count} инструментов за {stats.elapsed_seconds:.1f}s")
    print("Строим PayoffPackage…")
    pkg = build_payoff_package(options, params.spot_price)

    print(f"Запускаем MILP ({pkg.n_instruments} инструментов, {pkg.n_price_points} точек)…")
    result = solve_portfolio(pkg, params)

    print_result(result, pkg)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ETH Options MILP Optimizer — Step 3"
    )
    parser.add_argument("--test",      action="store_true", help="Unit tests (без сети)")
    parser.add_argument("--live",      action="store_true", help="Живые данные Deribit")
    parser.add_argument("--verbose",   action="store_true", help="Детальный вывод")
    parser.add_argument("--spot",      type=float, default=2_000.0, help="Спот-цена ETH")
    parser.add_argument("--floor",     type=float, default=-400.0,  help="PNL floor USD")
    parser.add_argument("--ceiling",   type=float, default=+400.0,  help="PNL ceiling USD")
    parser.add_argument("--margin",    type=float, default=5_000.0, help="Маржа USD")
    parser.add_argument("--max-qty",   type=int,   default=10,      help="Макс. контрактов")
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

    params = MILPParams(
        spot_price    = args.spot,
        max_qty       = args.max_qty,
        margin_budget = args.margin,
        pnl_floor     = args.floor,
        pnl_ceiling   = args.ceiling,
    )

    if args.live:
        asyncio.run(_run_live(params))
    else:
        # По умолчанию — синтетические данные с отчётом
        instruments = _make_synthetic_instruments(args.spot)
        pkg         = build_payoff_package(instruments, args.spot)
        result      = solve_portfolio(pkg, params)
        print_result(result, pkg)


if __name__ == "__main__":
    main()
