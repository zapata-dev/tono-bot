import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.conversation_logic import _format_price

PLACEHOLDER = "Precio por confirmar con un asesor"


def test_empty_price_never_renders_nd():
    assert _format_price("", "MXN", "") == PLACEHOLDER


def test_whitespace_price_is_placeholder():
    assert _format_price("   ", "", "TRUE") == PLACEHOLDER


def test_non_numeric_price_is_placeholder():
    assert _format_price("N/D", "MXN", "TRUE") == PLACEHOLDER


def test_numeric_price_unchanged():
    assert _format_price("1350000", "MXN", "TRUE") == "$1,350,000 MXN IVA incluido"
    assert _format_price("499,000.00", "", "") == "$499,000 MXN"


def test_spanish_boolean_iva():
    assert _format_price("699000", "MXN", "VERDADERO") == "$699,000 MXN IVA incluido"
    assert _format_price("699000", "MXN", "FALSO") == "$699,000 MXN"
