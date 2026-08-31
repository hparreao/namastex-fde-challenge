from autoseguro.pii import cep_prefix_from_text, find_pii, redact_pii


def test_redacts_supported_pii() -> None:
    raw = (
        "CPF 389.083.863-43, email teste@example.com, telefone +55 21 97224-2584, "
        "CEP 01310-100 e placa GGE4X30"
    )
    sanitized = redact_pii(raw)
    assert find_pii(sanitized) == set()
    assert "[CPF_REDACTED]" in sanitized
    assert "[EMAIL_REDACTED]" in sanitized
    assert "[PHONE_REDACTED]" in sanitized
    assert "[CEP_REDACTED]" in sanitized
    assert "[PLATE_REDACTED]" in sanitized


def test_extracts_only_cep_prefix() -> None:
    assert cep_prefix_from_text("CEP 01310-100") == "01"


def test_generated_identifiers_are_not_misclassified_as_phone_or_cep() -> None:
    identifiers = (
        "1ec4bcd9-0491-4979-838e-759fbff39852 trace_2c53ae1234567890d7bb3526f990142a 20260829_0003"
    )
    assert find_pii(identifiers) == set()
    assert redact_pii(identifiers) == identifiers
