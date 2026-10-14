import pytest

from builder import calc
from builder.credibility import rate, tier


def test_calculate():
    assert calc.calculate("(320 - 210 - 30 - 15) / 320 * 100") == 20.3125
    assert calc.calculate("200 * 15%") == 30
    assert calc.calculate("7 % 3") == 1
    assert calc.calculate("$1,200 * 12") == 14400
    assert calc.calculate("sqrt(16) + 2^3") == 12
    for bad in ('__import__("os")', "open('x')", "a + 1", "9 ** 99999"):
        with pytest.raises((ValueError, SyntaxError)):
            calc.calculate(bad)


def test_unit_economics_and_scenarios():
    ue = calc.unit_economics(45, 12, fees_pct=13, shipping=5, fixed_costs_per_month=300)
    assert ue["profit_per_unit"] == 22.15 and ue["breakeven_units_per_month"] == 14
    assert calc.unit_economics(10, 12)["breakeven_units_per_month"] is None
    s = calc.scenarios([{"name": "low", "units_per_month": 20, "price": 45, "unit_cost": 12,
                         "fixed_costs_per_month": 100}], startup_costs=500)[0]
    assert s["revenue_per_month"] == 900 and s["profit_per_month"] == 560 and s["payback_months"] == 1


def test_credibility_tiers():
    assert tier("https://www.bls.gov/x") == 1
    assert tier("https://pubmed.ncbi.nlm.nih.gov/1") == 1
    assert tier("https://ox.ac.uk/paper") == 1
    assert tier("https://arxiv.org/abs/1") == 2
    assert tier("https://www.reuters.com/a") == 2
    assert tier("https://en.wikipedia.org/wiki/X") == 3
    assert tier("https://old.reddit.com/r/x") == 4
    assert tier("http://abcdefghijklmnop.onion/") == 4
    assert tier("not a url") == 4
    assert rate("https://www.bls.gov/x")["domain"] == "bls.gov"
