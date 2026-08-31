from autoseguro.domain import Intent
from autoseguro.extraction import deterministic_decision, deterministic_extract


def test_extracts_quote_fields() -> None:
    data = deterministic_extract(
        "Meu veículo é um Toyota Corolla 2022, tenho 35 anos, CEP 01310-100, plano completo"
    )
    assert data.vehicle_model == "Toyota Corolla"
    assert data.vehicle_year == 2022
    assert data.age == 35
    assert data.cep_prefix == "01"
    assert data.plan_id == "completo"


def test_classifies_human_and_negotiation() -> None:
    assert deterministic_decision("quero falar com atendente").intent is Intent.HUMAN
    assert deterministic_decision("a concorrente está mais barata").intent is Intent.NEGOTIATE
