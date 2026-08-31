from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

QUOTE_SERVICE = Path(__file__).resolve().parents[2] / "quote-service"
sys.path.insert(0, str(QUOTE_SERVICE))

from app.quote_logic import CotacaoRecusada, _pro_rata_primeiro_mes, cotar  # noqa: E402


def test_complete_plan_and_high_risk_multiplier() -> None:
    result = cotar({"plano_id": "completo", "idade": 35, "veiculo_ano": 2022, "cep": "07"})
    assert result["premio_mensal"] == round(209.90 * 1.30, 2)
    assert result["franquia"] == 3000


def test_age_above_limit_is_rejected() -> None:
    with pytest.raises(CotacaoRecusada, match="75 anos"):
        cotar({"plano_id": "essencial", "idade": 76, "veiculo_ano": 2022})


def test_vehicle_over_twenty_years_is_rejected() -> None:
    old_year = date.today().year - 21
    with pytest.raises(CotacaoRecusada, match="mais de 20 anos"):
        cotar({"plano_id": "premium", "idade": 35, "veiculo_ano": old_year})


def test_first_payment_pro_rata() -> None:
    result = _pro_rata_primeiro_mes(310.0, date(2026, 7, 16))
    assert result == {"dias_no_mes": 31, "dias_cobrados": 16, "valor_primeiro_pagamento": 160.0}
