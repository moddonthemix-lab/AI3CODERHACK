"""Business math the model is not allowed to do in its head.

``calculate`` evaluates plain arithmetic safely (no names, no calls except a
few math functions). ``unit_economics`` and ``scenarios`` turn price/cost
assumptions into margins, break-even points and monthly profit, so every
number in a plan is reproducible.
"""

from __future__ import annotations

import ast
import re
import math
import operator

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
        ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp,
          "abs": abs, "round": round, "min": min, "max": max, "ceil": math.ceil, "floor": math.floor}
_CONSTS = {"pi": math.pi, "e": math.e}


def calculate(expression: str) -> float:
    """Evaluate arithmetic like ``(320 - 210 - 30 - 15) / 320 * 100`` or ``1.08 ** 5``."""
    expr = expression.replace(",", "").replace("$", "").replace("^", "**").strip()
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*%(?!\s*[\d(])", r"(\1/100)", expr)  # "15%" -> 0.15; "7 % 3" stays modulo
    tree = ast.parse(expr, mode="eval")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                raise ValueError("exponent too large")
            return _BIN[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS
                and not node.keywords):
            return _FUNCS[node.func.id](*(ev(a) for a in node.args))
        raise ValueError(f"not allowed in a calculation: {ast.dump(node)[:60]}")

    result = ev(tree)
    return round(result, 6) if isinstance(result, float) else result


def unit_economics(price: float, unit_cost: float, fees_pct: float = 0, fee_fixed: float = 0,
                   shipping: float = 0, fixed_costs_per_month: float = 0) -> dict:
    """Profit per unit, margin and how many units per month cover fixed costs."""
    fees = price * fees_pct / 100 + fee_fixed
    profit = price - unit_cost - fees - shipping
    margin = profit / price * 100 if price else 0.0
    if profit > 0:
        breakeven = math.ceil(fixed_costs_per_month / profit) if fixed_costs_per_month else 0
    else:
        breakeven = None  # every sale loses money — no volume fixes that
    return {
        "price": round(price, 2), "unit_cost": round(unit_cost, 2), "fees": round(fees, 2),
        "shipping": round(shipping, 2), "profit_per_unit": round(profit, 2),
        "margin_pct": round(margin, 1), "breakeven_units_per_month": breakeven,
        "markup_pct": round((price - unit_cost) / unit_cost * 100, 1) if unit_cost else None,
    }


def scenarios(items: list[dict], startup_costs: float = 0) -> list[dict]:
    """Monthly revenue/profit for named cases, e.g. pessimistic / expected / optimistic.

    Each item: name, units_per_month, price, unit_cost, fees_pct?, fixed_costs_per_month?
    """
    out = []
    for s in items:
        units = float(s.get("units_per_month", 0) or 0)
        ue = unit_economics(float(s.get("price", 0) or 0), float(s.get("unit_cost", 0) or 0),
                            float(s.get("fees_pct", 0) or 0), 0, float(s.get("shipping", 0) or 0),
                            float(s.get("fixed_costs_per_month", 0) or 0))
        revenue = units * ue["price"]
        profit = units * ue["profit_per_unit"] - float(s.get("fixed_costs_per_month", 0) or 0)
        payback = math.ceil(startup_costs / profit) if startup_costs and profit > 0 else None
        out.append({"name": s.get("name", f"case {len(out) + 1}"), "units_per_month": units,
                    "revenue_per_month": round(revenue, 2), "profit_per_month": round(profit, 2),
                    "margin_pct": ue["margin_pct"], "payback_months": payback,
                    "assumptions": s.get("assumptions", "")})
    return out


def _draw(rng, spec) -> float:
    """A number, or a range [low, high] / [low, likely, high] drawn uniformly / triangularly."""
    if isinstance(spec, (int, float)):
        return float(spec)
    if isinstance(spec, dict):
        spec = [spec.get("low", 0), spec.get("likely", None), spec.get("high", 0)]
        spec = [x for x in spec if x is not None]
    vals = [float(x) for x in spec]
    if len(vals) == 1:
        return vals[0]
    if len(vals) == 2:
        return rng.uniform(min(vals), max(vals))
    low, likely, high = vals[0], vals[1], vals[2]
    return rng.triangular(min(low, high), max(low, high), likely)


def monte_carlo(price, unit_cost, units_per_month, fees_pct=0, shipping=0, fixed_costs_per_month=0,
                startup_costs=0, months: int = 12, runs: int = 5000, seed: int = 7) -> dict:
    """Stress-test a business idea: every input may be a range, e.g. units_per_month=[5, 20, 60].

    Returns the distribution of monthly profit and first-year cash, and the probability of losing money,
    so a plan rests on "how likely" rather than on one optimistic guess.
    """
    import random
    import statistics

    rng = random.Random(seed)
    monthly, year = [], []
    for _ in range(max(100, min(runs, 50_000))):
        p, c = _draw(rng, price), _draw(rng, unit_cost)
        u, f = max(0.0, _draw(rng, units_per_month)), _draw(rng, fees_pct)
        sh, fx = _draw(rng, shipping), _draw(rng, fixed_costs_per_month)
        m = u * (p - c - p * f / 100 - sh) - fx
        monthly.append(m)
        year.append(m * months - _draw(rng, startup_costs))
    monthly.sort()
    year.sort()
    pct = lambda xs, q: round(xs[min(len(xs) - 1, int(q * len(xs)))], 2)  # noqa: E731
    return {
        "runs": len(monthly), "months": months,
        "monthly_profit": {"p10": pct(monthly, 0.10), "median": pct(monthly, 0.5), "p90": pct(monthly, 0.90),
                           "mean": round(statistics.fmean(monthly), 2)},
        "first_period_cash": {"p10": pct(year, 0.10), "median": pct(year, 0.5), "p90": pct(year, 0.90)},
        "prob_monthly_loss": round(sum(m < 0 for m in monthly) / len(monthly), 3),
        "prob_not_recovering_startup": round(sum(y < 0 for y in year) / len(year), 3),
    }
