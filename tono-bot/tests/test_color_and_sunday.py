import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.conversation_fsm import _extract_appointment
from src.conversation_logic import _build_focused_inventory_text


class _Inv:
    items = [
        {"Marca": "FREIGHTLINER", "Modelo": "CASCADIA 125", "Anio": "2020", "Color": "AZUL", "Precio": "", "condicion": "SEMINUEVO"},
        {"Marca": "FREIGHTLINER", "Modelo": "CASCADIA 125", "Anio": "2020", "Color": "BLANCO", "Precio": "1350000", "moneda": "MXN", "condicion": "SEMINUEVO"},
    ]


def test_focused_inventory_includes_color():
    text = _build_focused_inventory_text(_Inv(), "Cascadia 2020")
    assert "Color: AZUL" in text
    assert "Color: BLANCO" in text


def test_sunday_is_not_an_appointment():
    assert _extract_appointment("Puedo ir el domingo") is None
    assert _extract_appointment("el domingo a las 10") is None


def test_weekdays_still_work():
    assert _extract_appointment("mejor el lunes") == "Lunes"
    assert _extract_appointment("sabado 11am") == "Sábado 11:00 AM"
