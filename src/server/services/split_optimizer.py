from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp


@dataclass(slots=True)
class OfferCandidate:
    """Предложение поставщика по конкретной эталонной шине."""

    offer_item_id: int
    tire_id: int
    sku: str | None
    price: float
    stock_qty: int
    lot_size: int = 1
    min_order_qty: int = 1
    supplier_id: int | None = None
    supplier_name: str | None = None
    supplier_article: str | None = None


@dataclass(slots=True)
class SplitOptimizationOutput:
    """Результат работы алгоритма SplitOptimizer."""

    solver_method: str
    allocations: list[dict[str, Any]]
    total_cost: float
    baseline_cost: float | None
    savings: float
    savings_percent: float
    unfulfilled: list[dict[str, Any]]


class SplitOptimizerService:
    """
    Сервис оптимизации распределения закупки между поставщиками (SplitOptimizer).
    Решает задачу смешанного целочисленного программирования (MILP) на базе scipy.optimize.milp
    с учетом остатков, квантов поставки (lot_size), минимальных партий (min_order_qty)
    и опциональной минимальной суммы заказа на поставщика, с fallback на жадную эвристику.
    """

    @classmethod
    def optimize(
        cls,
        demands: dict[int, int],
        offers: list[OfferCandidate],
        min_supplier_order_amount: float = 0.0,
        timeout_seconds: float = 10.0,
    ) -> SplitOptimizationOutput:
        """Находит оптимальный сплит заказа и рассчитывает экономию относительно одного поставщика."""
        clean_demands = {int(tid): int(qty) for tid, qty in demands.items() if int(qty) > 0}
        if not clean_demands:
            return SplitOptimizationOutput(
                solver_method="milp",
                allocations=[],
                total_cost=0.0,
                baseline_cost=0.0,
                savings=0.0,
                savings_percent=0.0,
                unfulfilled=[],
            )

        # Отбираем только валидные офферы по требуемым шинам, где хватает остатка хотя бы на 1 квант и min_order_qty
        valid_offers: list[OfferCandidate] = []
        for off in offers:
            if off.tire_id not in clean_demands or off.price <= 0 or off.stock_qty <= 0:
                continue
            lot = max(1, int(off.lot_size))
            moq = max(1, int(off.min_order_qty))
            max_lots = off.stock_qty // lot
            if max_lots <= 0 or max_lots * lot < moq:
                continue
            valid_offers.append(
                OfferCandidate(
                    offer_item_id=off.offer_item_id,
                    tire_id=off.tire_id,
                    sku=off.sku,
                    price=float(off.price),
                    stock_qty=int(off.stock_qty),
                    lot_size=lot,
                    min_order_qty=moq,
                    supplier_id=off.supplier_id,
                    supplier_name=off.supplier_name,
                    supplier_article=off.supplier_article,
                )
            )

        if not valid_offers:
            unfulfilled = [
                {
                    "tire_id": tid,
                    "required_qty": req,
                    "allocated_qty": 0,
                    "shortage_qty": req,
                }
                for tid, req in sorted(clean_demands.items())
            ]
            return SplitOptimizationOutput(
                solver_method="milp",
                allocations=[],
                total_cost=0.0,
                baseline_cost=None,
                savings=0.0,
                savings_percent=0.0,
                unfulfilled=unfulfilled,
            )

        milp_result = cls._solve_milp(
            demands=clean_demands,
            offers=valid_offers,
            min_supplier_order_amount=min_supplier_order_amount,
            timeout_seconds=timeout_seconds,
        )
        if milp_result is not None:
            lots_chosen, solver_method = milp_result, "milp"
        else:
            lots_chosen = cls._solve_greedy(demands=clean_demands, offers=valid_offers)
            solver_method = "greedy"

        allocations: list[dict[str, Any]] = []
        allocated_by_tire: dict[int, int] = {tid: 0 for tid in clean_demands}
        total_cost = 0.0

        for off, lots in zip(valid_offers, lots_chosen):
            if lots <= 0:
                continue
            qty = lots * off.lot_size
            line_total = round(qty * off.price, 2)
            total_cost += line_total
            allocated_by_tire[off.tire_id] = allocated_by_tire.get(off.tire_id, 0) + qty
            allocations.append(
                {
                    "tire_id": off.tire_id,
                    "sku": off.sku,
                    "offer_item_id": off.offer_item_id,
                    "supplier_id": off.supplier_id,
                    "supplier_name": off.supplier_name,
                    "supplier_article": off.supplier_article,
                    "qty": qty,
                    "lots": lots,
                    "lot_size": off.lot_size,
                    "price": round(off.price, 2),
                    "total": line_total,
                }
            )

        total_cost = round(total_cost, 2)

        unfulfilled: list[dict[str, Any]] = []
        for tid, req in sorted(clean_demands.items()):
            got = allocated_by_tire.get(tid, 0)
            if got < req:
                unfulfilled.append(
                    {
                        "tire_id": tid,
                        "required_qty": req,
                        "allocated_qty": got,
                        "shortage_qty": req - got,
                    }
                )

        # Целевой объем для расчета честного baseline (по фактически покрываемой потребности)
        fulfilled_targets = {
            tid: min(req, allocated_by_tire.get(tid, 0))
            for tid, req in clean_demands.items()
            if min(req, allocated_by_tire.get(tid, 0)) > 0
        }

        baseline_cost = cls._compute_baseline_cost(
            targets=fulfilled_targets,
            offers=valid_offers,
            allocations=allocations,
        )

        if baseline_cost is not None and baseline_cost > 0:
            savings = round(max(0.0, baseline_cost - total_cost), 2)
            savings_percent = round((savings / baseline_cost) * 100.0, 2) if savings > 0 else 0.0
        else:
            savings = 0.0
            savings_percent = 0.0

        return SplitOptimizationOutput(
            solver_method=solver_method,
            allocations=allocations,
            total_cost=total_cost,
            baseline_cost=baseline_cost,
            savings=savings,
            savings_percent=savings_percent,
            unfulfilled=unfulfilled,
        )

    @classmethod
    def _solve_milp(
        cls,
        demands: dict[int, int],
        offers: list[OfferCandidate],
        min_supplier_order_amount: float,
        timeout_seconds: float,
    ) -> list[int] | None:
        """
        Формулирует и решает задачу MILP:
        переменные: [x_0..x_{K-1}, s_0..s_{M-1}, z_0..z_{B-1}, y_0..y_{S-1}],
        где x_k — число квантов оффера k, s_i — дефицит по шине i (со штрафом Big-M),
        z_b — бинарные индикаторы min_order_qty > lot_size,
        y_j — бинарные индикаторы выбора поставщика при min_supplier_order_amount > 0.
        """
        tire_ids = sorted(demands.keys())
        tire_index = {tid: idx for idx, tid in enumerate(tire_ids)}

        k_count = len(offers)
        m_count = len(tire_ids)

        # Офферы, требующие бинарной переменной для min_order_qty
        moq_offer_indices: list[tuple[int, int]] = []  # (offer_idx, min_lots)
        for idx, off in enumerate(offers):
            min_lots = math.ceil(off.min_order_qty / off.lot_size)
            if min_lots > 1:
                moq_offer_indices.append((idx, min_lots))
        b_count = len(moq_offer_indices)

        # Группировка по поставщику для min_supplier_order_amount
        supplier_groups: list[list[int]] = []
        if min_supplier_order_amount > 0:
            by_sup: dict[tuple[str, int], list[int]] = {}
            for idx, off in enumerate(offers):
                key = ("sup", off.supplier_id) if off.supplier_id is not None else ("item", off.offer_item_id)
                by_sup.setdefault(key, []).append(idx)
            supplier_groups = list(by_sup.values())
        s_count = len(supplier_groups)

        total_vars = k_count + m_count + b_count + s_count

        # Штраф Big-M за каждую единицу непокрытого спроса строго больше стоимости всего склада
        max_universe_cost = sum(off.price * off.stock_qty for off in offers)
        big_m = max_universe_cost * 10.0 + 1_000_000.0

        c = np.zeros(total_vars, dtype=float)
        lb = np.zeros(total_vars, dtype=float)
        ub = np.zeros(total_vars, dtype=float)
        integrality = np.ones(total_vars, dtype=int)

        max_lots_list: list[int] = []
        for idx, off in enumerate(offers):
            max_lots = off.stock_qty // off.lot_size
            max_lots_list.append(max_lots)
            c[idx] = off.price * off.lot_size
            ub[idx] = float(max_lots)

        for i_idx, tid in enumerate(tire_ids):
            var_pos = k_count + i_idx
            c[var_pos] = big_m
            ub[var_pos] = float(demands[tid])

        for b_idx in range(b_count):
            var_pos = k_count + m_count + b_idx
            ub[var_pos] = 1.0

        for j_idx in range(s_count):
            var_pos = k_count + m_count + b_count + j_idx
            ub[var_pos] = 1.0

        rows: list[np.ndarray] = []
        row_lb: list[float] = []
        row_ub: list[float] = []

        # 1. Ограничения покрытия спроса: sum_{k in O_i} lot_k * x_k + s_i >= demand_i
        for i_idx, tid in enumerate(tire_ids):
            row = np.zeros(total_vars, dtype=float)
            for k_idx, off in enumerate(offers):
                if off.tire_id == tid:
                    row[k_idx] = float(off.lot_size)
            row[k_count + i_idx] = 1.0
            rows.append(row)
            row_lb.append(float(demands[tid]))
            row_ub.append(np.inf)

        # 2. Полунепрерывные ограничения min_order_qty: min_lots * z_b <= x_k <= max_lots * z_b
        for b_idx, (k_idx, min_lots) in enumerate(moq_offer_indices):
            z_pos = k_count + m_count + b_idx
            max_lots = max_lots_list[k_idx]

            # x_k - max_lots * z_b <= 0
            r1 = np.zeros(total_vars, dtype=float)
            r1[k_idx] = 1.0
            r1[z_pos] = -float(max_lots)
            rows.append(r1)
            row_lb.append(-np.inf)
            row_ub.append(0.0)

            # -x_k + min_lots * z_b <= 0
            r2 = np.zeros(total_vars, dtype=float)
            r2[k_idx] = -1.0
            r2[z_pos] = float(min_lots)
            rows.append(r2)
            row_lb.append(-np.inf)
            row_ub.append(0.0)

        # 3. Минимальная сумма заказа на поставщика
        for j_idx, offer_idxs in enumerate(supplier_groups):
            y_pos = k_count + m_count + b_count + j_idx
            sum_max_lots = sum(max_lots_list[k] for k in offer_idxs)

            # sum x_k - sum_max_lots * y_j <= 0
            r1 = np.zeros(total_vars, dtype=float)
            for k in offer_idxs:
                r1[k] = 1.0
            r1[y_pos] = -float(sum_max_lots)
            rows.append(r1)
            row_lb.append(-np.inf)
            row_ub.append(0.0)

            # -sum (price_k * lot_k) * x_k + min_supplier_order_amount * y_j <= 0
            r2 = np.zeros(total_vars, dtype=float)
            for k in offer_idxs:
                r2[k] = -(offers[k].price * offers[k].lot_size)
            r2[y_pos] = float(min_supplier_order_amount)
            rows.append(r2)
            row_lb.append(-np.inf)
            row_ub.append(0.0)

        A = np.vstack(rows)
        constraints = LinearConstraint(A, lb=np.array(row_lb), ub=np.array(row_ub))
        bounds = Bounds(lb=lb, ub=ub)

        try:
            res = milp(
                c=c,
                integrality=integrality,
                bounds=bounds,
                constraints=constraints,
                options={"time_limit": float(timeout_seconds)},
            )
            if res.success and res.x is not None:
                return [int(round(val)) for val in res.x[:k_count]]
        except Exception:
            return None
        return None

    @classmethod
    def _solve_greedy(
        cls,
        demands: dict[int, int],
        offers: list[OfferCandidate],
    ) -> list[int]:
        """Жадная эвристика сплита заказа по возрастанию цены с учетом квантов и остатков."""
        lots_result = [0] * len(offers)
        for tid, req_qty in demands.items():
            remaining = req_qty
            indexed = [
                (idx, off)
                for idx, off in enumerate(offers)
                if off.tire_id == tid
            ]
            indexed.sort(key=lambda pair: (pair[1].price, pair[1].lot_size))

            for idx, off in indexed:
                if remaining <= 0:
                    break
                max_lots = off.stock_qty // off.lot_size
                min_lots = math.ceil(off.min_order_qty / off.lot_size)
                if max_lots < min_lots:
                    continue
                needed_lots = math.ceil(remaining / off.lot_size)
                take_lots = min(max_lots, max(min_lots, needed_lots))
                lots_result[idx] = take_lots
                remaining -= take_lots * off.lot_size

        return lots_result

    @classmethod
    def _single_offer_cost_for_qty(cls, off: OfferCandidate, target_qty: int) -> float | None:
        """Стоимость закрытия target_qty одним конкретным оффером (либо None, если остатка не хватает)."""
        max_lots = off.stock_qty // off.lot_size
        min_lots = math.ceil(off.min_order_qty / off.lot_size)
        needed_lots = math.ceil(target_qty / off.lot_size)
        req_lots = max(min_lots, needed_lots)
        if req_lots > max_lots:
            return None
        return round(req_lots * off.lot_size * off.price, 2)

    @classmethod
    def _compute_baseline_cost(
        cls,
        targets: dict[int, int],
        offers: list[OfferCandidate],
        allocations: list[dict[str, Any]],
    ) -> float | None:
        """
        Рассчитывает baseline-стоимость закупки без сплита:
        1) минимальная стоимость покупки всей корзины у ОДНОГО поставщика, способного закрыть все позиции;
        2) если ни один поставщик не закрывает всю корзину целиком — сумма лучших одиночных вариантов по каждой шине
           (или по максимальной цене в сплите при дефиците у каждого отдельного поставщика).
        """
        if not targets:
            return 0.0

        # Группируем по поставщикам
        by_supplier: dict[tuple[str, int], list[OfferCandidate]] = {}
        for off in offers:
            key = ("sup", off.supplier_id) if off.supplier_id is not None else ("item", off.offer_item_id)
            by_supplier.setdefault(key, []).append(off)

        full_basket_costs: list[float] = []
        for sup_offers in by_supplier.values():
            basket_cost = 0.0
            possible = True
            for tid, qty in targets.items():
                best_for_tire: float | None = None
                for off in sup_offers:
                    if off.tire_id != tid:
                        continue
                    cost = cls._single_offer_cost_for_qty(off, qty)
                    if cost is not None and (best_for_tire is None or cost < best_for_tire):
                        best_for_tire = cost
                if best_for_tire is None:
                    possible = False
                    break
                basket_cost += best_for_tire
            if possible:
                full_basket_costs.append(round(basket_cost, 2))

        if full_basket_costs:
            return min(full_basket_costs)

        # Fallback: ни один одиночный поставщик не закрывает всю корзину целиком
        per_tire_sum = 0.0
        for tid, qty in targets.items():
            single_costs = [
                cost
                for off in offers
                if off.tire_id == tid
                and (cost := cls._single_offer_cost_for_qty(off, qty)) is not None
            ]
            if single_costs:
                per_tire_sum += min(single_costs)
            else:
                # Ни у одного поставщика нет полного объема даже по одной этой шине — берем макс. цену из сплита
                used_prices = [float(a["price"]) for a in allocations if a["tire_id"] == tid]
                if not used_prices:
                    return None
                per_tire_sum += round(max(used_prices) * qty, 2)

        return round(per_tire_sum, 2)
