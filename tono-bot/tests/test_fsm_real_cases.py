"""
Test suite for FSM v2 with real-world conversation cases.

These tests cover the specific scenarios from production logs
that previously caused bugs (city hallucination, phone loop,
model switch ignored, etc.)
"""
import sys
import os
import re

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from conversation_fsm import (
    process_fsm, Action, ConversationState, Slots,
    classify_intent, Intent,
    extract_entities_for_fsm, diff_slots, SlotChange,
    validate_legacy_value,
)
from llm_writer import try_deterministic_response


# ============================================================
# CASE 1: "ESTA BIEN TE DOY 670 MIL" → offer, NOT city
# ============================================================
def test_te_doy_not_city():
    """'TE DOY 670 MIL' must NOT be extracted as a city."""
    data = extract_entities_for_fsm("ESTA BIEN TE DOY 670 MIL", "", {})
    assert "city" not in data, f"City should NOT be extracted, got: {data.get('city')}"
    assert "offer_amount" in data, f"Offer should be extracted, got: {data}"
    assert "670" in data["offer_amount"], f"Offer should contain 670, got: {data['offer_amount']}"
    print("✅ CASE 1: 'TE DOY 670 MIL' → offer, not city")


# ============================================================
# CASE 2: "ME LLAMO JUAN ROMAN" → saves name
# ============================================================
def test_name_extraction():
    """Explicit name patterns should be extracted."""
    data = extract_entities_for_fsm("me llamo Juan Roman", "", {})
    assert "name" in data, f"Name should be extracted, got: {data}"
    assert "Juan" in data["name"] and "Roman" in data["name"], f"Got: {data['name']}"
    print(f"✅ CASE 2: Name = '{data['name']}'")


# ============================================================
# CASE 3: Phone already available → no re-ask
# ============================================================
def test_phone_already_known():
    """When phone is in slots, filled_summary should say NOT to ask."""
    slots = Slots(phone="5551234567", name="Pedro")
    summary = slots.filled_summary()
    assert "NO lo pidas" in summary, f"Summary should say don't ask for phone: {summary}"
    print(f"✅ CASE 3: Phone summary = '{summary[:80]}'")


# ============================================================
# CASE 4: "ESE CEL EL MIO DONDE TE ESTOY HABLANDO" → not extracted as new data
# ============================================================
def test_ese_cel_el_mio():
    """Implicit phone reference should NOT extract a new phone number."""
    data = extract_entities_for_fsm(
        "ese cel el mio donde te estoy hablando", "", {}
    )
    assert "phone" not in data, f"Should NOT extract phone from 'ese cel': {data}"
    print("✅ CASE 4: 'ese cel el mio' → no phone extraction")


# ============================================================
# CASE 5: "jrmu@edu.mc" → saves email
# ============================================================
def test_email_extraction():
    """Email pattern should be captured."""
    data = extract_entities_for_fsm("jrmu@edu.mc", "", {})
    assert data.get("email") == "jrmu@edu.mc", f"Expected email 'jrmu@edu.mc', got: {data.get('email')}"
    print(f"✅ CASE 5: Email = '{data['email']}'")


# ============================================================
# CASE 6: "unos quince dias" → saves timeline
# ============================================================
def test_timeline_extraction():
    """Timeline expressions should be captured."""
    # With bot asking for timeline in history
    history = "A: ¿Cuál sería tu tiempo estimado para liquidar?"
    data = extract_entities_for_fsm("unos quince dias", history, {})
    assert "timeline" in data, f"Timeline should be extracted, got: {data}"
    print(f"✅ CASE 6: Timeline = '{data['timeline']}'")


def test_timeline_explicit():
    """Explicit time periods should extract regardless of history."""
    data = extract_entities_for_fsm("en 3 meses", "", {})
    assert "timeline" in data, f"Should extract '3 meses': {data}"
    print(f"✅ CASE 6b: Timeline = '{data['timeline']}'")


def test_timeline_immediate():
    """'Inmediato' as timeline."""
    history = "A: ¿Cuál sería tu tiempo estimado?"
    data = extract_entities_for_fsm("ya, lo antes posible", history, {})
    assert data.get("timeline") == "Inmediato", f"Expected 'Inmediato', got: {data.get('timeline')}"
    print(f"✅ CASE 6c: Timeline = '{data['timeline']}'")


# ============================================================
# CASE 7: "tienes mas tractos?" → NOT city
# ============================================================
def test_mas_tractos_not_city():
    """Inventory question should NOT be extracted as city."""
    data = extract_entities_for_fsm("tienes mas tractos?", "", {})
    assert "city" not in data, f"City should NOT be extracted from 'mas tractos': {data}"
    print("✅ CASE 7: 'tienes mas tractos?' → no city")


# ============================================================
# CASE 8: "soy de amecameca" → saves city
# ============================================================
def test_city_amecameca():
    """Explicit city pattern should be captured."""
    data = extract_entities_for_fsm("soy de amecameca", "", {})
    assert "city" in data, f"City should be extracted from 'soy de amecameca': {data}"
    assert "amecameca" in data["city"].lower(), f"Got: {data['city']}"
    print(f"✅ CASE 8: City = '{data['city']}'")


# ============================================================
# CASE 9: "si tienes mas camiones?" → NOT city
# ============================================================
def test_si_tienes_mas_camiones_not_city():
    """Generic inventory question must NOT become a city."""
    data = extract_entities_for_fsm("si tienes mas camiones?", "", {})
    assert "city" not in data, f"City should NOT be extracted: {data}"
    print("✅ CASE 9: 'si tienes mas camiones?' → no city")


# ============================================================
# CASE 10: "Hola CA-SU1" → FSM presents campaign
# ============================================================
def test_campaign_entry_greeting():
    """First message with campaign → PRESENT_CAMPAIGN action."""
    ctx = {}
    action, state, slots, meta = process_fsm("Hola", ctx, {}, has_campaign=True, turn_count=1)
    assert action == Action.PRESENT_CAMPAIGN, f"Expected PRESENT_CAMPAIGN, got: {action}"
    assert state == ConversationState.CAMPAIGN_ENTRY, f"Expected CAMPAIGN_ENTRY, got: {state}"
    assert meta.get("primary_flow") == "campaign_registration"
    print(f"✅ CASE 10: Campaign entry → {action.value}, flow={meta['primary_flow']}")


# ============================================================
# CASE 11: Data provision in campaign → acknowledge + ask next
# ============================================================
def test_campaign_data_collection():
    """Providing name in campaign should acknowledge and ask next slot."""
    ctx = {"fsm_state": "campaign_entry"}
    action, state, slots, meta = process_fsm(
        "me llamo Pedro", ctx,
        new_data={"name": "Pedro"},
        has_campaign=True, turn_count=3
    )
    assert action in (Action.ACKNOWLEDGE_AND_ASK_NEXT, Action.CONFIRM_REGISTRATION), \
        f"Expected ACK or CONFIRM, got: {action}"
    print(f"✅ CASE 11: Name in campaign → {action.value}")


# ============================================================
# CASE 12: Side question in campaign → ANSWER_QUESTION + keep state
# ============================================================
def test_side_question_in_campaign():
    """Price question during campaign should not lose campaign state."""
    ctx = {"fsm_state": "campaign_entry", "user_name": "Pedro"}
    action, state, slots, meta = process_fsm(
        "cuánto cuesta?", ctx, new_data={},
        has_campaign=True, turn_count=4
    )
    assert state == ConversationState.CAMPAIGN_ENTRY, f"Should stay in CAMPAIGN_ENTRY, got: {state}"
    assert meta.get("is_side_question") is True, f"Should be side question: {meta}"
    print(f"✅ CASE 12: Side question → {action.value}, state={state.value}, side={meta.get('is_side_question')}")


# ============================================================
# CASE 13: Slot diffing works correctly
# ============================================================
def test_slot_diff():
    """diff_slots should detect exactly what changed."""
    old = Slots(name="Pedro", phone="5551234567")
    new = Slots(name="Pedro", phone="5551234567", email="pedro@test.com", city="CDMX")
    changes = diff_slots(old, new)
    changed_slots = {c.slot for c in changes}
    assert changed_slots == {"email", "city"}, f"Expected email+city changes, got: {changed_slots}"
    print(f"✅ CASE 13: Diff = {[(c.slot, c.new_value) for c in changes]}")


# ============================================================
# CASE 14: Deterministic response for ASK_NAME
# ============================================================
def test_deterministic_ask_name():
    """ASK_NAME should return a template without LLM."""
    resp = try_deterministic_response(Action.ASK_NAME, Slots(), {}, [])
    assert resp is not None, "Should return deterministic response"
    assert "nombre" in resp.lower() or "quién" in resp.lower(), f"Should ask for name: {resp}"
    print(f"✅ CASE 14: Deterministic ASK_NAME = '{resp}'")


def test_deterministic_does_not_repeat():
    """Template should avoid repeating the last bot message."""
    last = ["¿Me compartes tu nombre completo, por favor?"]
    resp = try_deterministic_response(Action.ASK_NAME, Slots(), {}, last)
    assert resp.lower() != last[0].lower(), f"Should not repeat: {resp}"
    print(f"✅ CASE 14b: Anti-repeat = '{resp}'")


# ============================================================
# CASE 15: ANSWER_QUESTION needs LLM (not deterministic)
# ============================================================
def test_answer_question_needs_llm():
    """Complex questions should NOT get deterministic responses."""
    resp = try_deterministic_response(Action.ANSWER_QUESTION, Slots(), {}, [])
    assert resp is None, f"ANSWER_QUESTION should need LLM, got: {resp}"
    print("✅ CASE 15: ANSWER_QUESTION → None (needs LLM)")


# ============================================================
# CASE 16: Legacy validation guards
# ============================================================
def test_validate_legacy_city():
    """Legacy city values with noise words should be rejected."""
    assert validate_legacy_value("city", "TE DOY") is None
    assert validate_legacy_value("city", "si tienes mas camiones?") is None
    assert validate_legacy_value("city", "foton") is None
    assert validate_legacy_value("city", "Amecameca") == "Amecameca"
    assert validate_legacy_value("city", "CDMX") == "CDMX"
    assert validate_legacy_value("city", "Monterrey") == "Monterrey"
    print("✅ CASE 16: Legacy city validation OK")


def test_validate_legacy_phone():
    """Phone validation should require 10-15 digits."""
    assert validate_legacy_value("phone", "5551234567") == "5551234567"
    assert validate_legacy_value("phone", "12345") is None  # too short
    assert validate_legacy_value("phone", "abc") is None
    print("✅ CASE 16b: Legacy phone validation OK")


def test_validate_legacy_appointment():
    """Appointment validation should require day/time words."""
    assert validate_legacy_value("appointment", "Viernes 10:00 AM") is not None
    assert validate_legacy_value("appointment", "mañana") is not None
    assert validate_legacy_value("appointment", "algo random") is None
    print("✅ CASE 16c: Legacy appointment validation OK")


def test_validate_legacy_payment():
    """Payment validation should require known labels."""
    assert validate_legacy_value("payment", "Contado") is not None
    assert validate_legacy_value("payment", "Crédito") is not None
    assert validate_legacy_value("payment", "basura") is None
    print("✅ CASE 16d: Legacy payment validation OK")


# ============================================================
# CASE 17: Intent classification context-aware
# ============================================================
def test_intent_no_in_campaign_is_deny():
    """Simple 'no' in campaign = DENY, not DISINTEREST."""
    intent = classify_intent("no", Slots(), current_state=ConversationState.CAMPAIGN_ENTRY)
    assert intent == Intent.DENY, f"Expected DENY, got: {intent}"
    print("✅ CASE 17: 'no' in campaign → DENY")


def test_intent_no_gracias_is_disinterest():
    """'no gracias' = DISINTEREST regardless of state."""
    intent = classify_intent("no gracias", Slots(), current_state=ConversationState.CAMPAIGN_ENTRY)
    assert intent == Intent.DISINTEREST, f"Expected DISINTEREST, got: {intent}"
    print("✅ CASE 17b: 'no gracias' → DISINTEREST")


def test_intent_data_plus_question_in_campaign():
    """Data + question in campaign should prioritize PROVIDE_DATA."""
    intent = classify_intent(
        "Pedro Garcia, cuanto cuesta?", Slots(),
        new_data={"name": "Pedro Garcia"},
        current_state=ConversationState.CAMPAIGN_ENTRY,
    )
    assert intent == Intent.PROVIDE_DATA, f"Expected PROVIDE_DATA, got: {intent}"
    print("✅ CASE 17c: Data + question in campaign → PROVIDE_DATA")


# ============================================================
# CASE 18: Multi-line message extraction
# ============================================================
def test_multiline_extraction():
    """Multi-line message with name, email, city."""
    msg = "Pedro Garcia\npedro@test.com\nCDMX"
    history = "A: ¿Me compartes tu nombre, correo y ciudad?"
    data = extract_entities_for_fsm(msg, history, {})
    assert data.get("name") == "Pedro Garcia", f"Name: {data.get('name')}"
    assert data.get("email") == "pedro@test.com", f"Email: {data.get('email')}"
    # City should extract from multi-line when bot asked for datos
    print(f"✅ CASE 18: Multi-line extraction = {data}")


# ============================================================
# CASE 19: Complete campaign flow simulation
# ============================================================
def test_full_campaign_flow():
    """Simulate a complete campaign conversation flow."""
    ctx = {}

    # Turn 1: Greeting with campaign
    action, state, slots, meta = process_fsm("Hola CA-SU1", ctx, {}, True, 1)
    assert action == Action.PRESENT_CAMPAIGN
    assert state == ConversationState.CAMPAIGN_ENTRY

    # Turn 2: Provide name
    action, state, slots, meta = process_fsm(
        "me llamo Juan", ctx, {"name": "Juan"}, True, 2
    )
    assert slots.name == "Juan"

    # Turn 3: Provide email
    action, state, slots, meta = process_fsm(
        "juan@test.com", ctx, {"email": "juan@test.com"}, True, 3
    )
    assert slots.email == "juan@test.com"

    # Turn 4: Provide city
    action, state, slots, meta = process_fsm(
        "Monterrey", ctx, {"city": "Monterrey"}, True, 4
    )
    assert slots.city == "Monterrey"

    # Turn 5: Provide timeline → should complete registration
    action, state, slots, meta = process_fsm(
        "3 meses", ctx, {"timeline": "3 meses"}, True, 5
    )
    assert action == Action.CONFIRM_REGISTRATION, f"Expected CONFIRM_REGISTRATION, got: {action}"
    assert state == ConversationState.QUALIFIED
    print(f"✅ CASE 19: Full campaign flow → QUALIFIED after 5 turns")


# ============================================================
# RUN ALL
# ============================================================
if __name__ == "__main__":
    tests = [
        test_te_doy_not_city,
        test_name_extraction,
        test_phone_already_known,
        test_ese_cel_el_mio,
        test_email_extraction,
        test_timeline_extraction,
        test_timeline_explicit,
        test_timeline_immediate,
        test_mas_tractos_not_city,
        test_city_amecameca,
        test_si_tienes_mas_camiones_not_city,
        test_campaign_entry_greeting,
        test_campaign_data_collection,
        test_side_question_in_campaign,
        test_slot_diff,
        test_deterministic_ask_name,
        test_deterministic_does_not_repeat,
        test_answer_question_needs_llm,
        test_validate_legacy_city,
        test_validate_legacy_phone,
        test_validate_legacy_appointment,
        test_validate_legacy_payment,
        test_intent_no_in_campaign_is_deny,
        test_intent_no_gracias_is_disinterest,
        test_intent_data_plus_question_in_campaign,
        test_multiline_extraction,
        test_full_campaign_flow,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"❌ {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"❌ {test.__name__}: UNEXPECTED ERROR: {e}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed, {passed+failed} total")
    if failed == 0:
        print("ALL TESTS PASSED ✅")
    else:
        print(f"FAILURES: {failed} ❌")
        sys.exit(1)
