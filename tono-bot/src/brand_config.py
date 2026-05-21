"""
Brand configuration loader.
Reads brand/ folder at startup and exposes config to the rest of the app.
"""
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

import yaml

logger = logging.getLogger(__name__)

# brand/ vive en la raíz del repo (dos niveles arriba de src/)
# tono-bot/src/brand_config.py → tono-bot-refactor/brand/
BRAND_DIR = Path(__file__).resolve().parent.parent.parent / "brand"


@lru_cache(maxsize=1)
def get_brand_config() -> Dict[str, Any]:
    """Carga y cachea toda la configuración de la marca."""
    if not BRAND_DIR.exists():
        raise RuntimeError(
            f"brand/ directory not found at {BRAND_DIR}. "
            "See brand/README.md for setup."
        )

    brand_yaml = BRAND_DIR / "brand.yaml"
    vehicles_yaml = BRAND_DIR / "vehicles.yaml"
    prompt_md = BRAND_DIR / "prompt.md"
    inventory_csv = BRAND_DIR / "inventory.csv"
    financing_json = BRAND_DIR / "financing.json"

    for required in (brand_yaml, vehicles_yaml):
        if not required.exists():
            raise RuntimeError(f"Required brand file missing: {required}")

    with open(brand_yaml, encoding="utf-8") as f:
        brand_data = yaml.safe_load(f)
    with open(vehicles_yaml, encoding="utf-8") as f:
        vehicles_data = yaml.safe_load(f)

    prompt_template = ""
    if prompt_md.exists():
        with open(prompt_md, encoding="utf-8") as f:
            prompt_template = f.read()

    cfg = {
        "brand": brand_data["brand"],
        "bot": brand_data["bot"],
        "business": brand_data["business"],
        "whatsapp": brand_data["whatsapp"],
        "crm": brand_data["crm"],
        "behavior": brand_data["behavior"],
        "llm": brand_data["llm"],
        "dropdown_map": vehicles_data["dropdown_map"],
        "tracking_codes": vehicles_data["tracking_codes"],
        "campaign_types": vehicles_data["campaign_types"],
        "payment_labels": vehicles_data.get("payment_labels", {}),
        "prompt_template": prompt_template,
        "inventory_path": str(inventory_csv) if inventory_csv.exists() else None,
        "financing_path": str(financing_json) if financing_json.exists() else None,
    }

    logger.info(
        f"✅ Brand loaded: {cfg['brand']['name']} "
        f"(persona={cfg['bot']['persona_name']}, "
        f"models={len(cfg['dropdown_map'])}, "
        f"tracking_codes={len(cfg['tracking_codes'])})"
    )
    return cfg


def render_system_prompt(office_maps_url_override: str = None, **runtime_vars) -> str:
    """
    Renderiza el system prompt con valores de marca + valores de runtime.
    Runtime vars (current_time_str, user_name_context, etc.) se pasan
    desde conversation_logic.py en cada turno.
    office_maps_url_override: si se pasa (env var OFFICE_MAPS_URL), reemplaza el valor de brand.yaml.
    """
    cfg = get_brand_config()
    if not cfg["prompt_template"]:
        raise RuntimeError(
            f"brand/prompt.md not found at {BRAND_DIR / 'prompt.md'}. "
            "Create it from the SYSTEM_PROMPT template."
        )
    effective_maps_url = (
        office_maps_url_override.strip()
        if office_maps_url_override and office_maps_url_override.strip()
        else cfg["business"]["office_maps_url"]
    )
    all_vars = {
        "brand_name": cfg["brand"]["name"],
        "persona_name": cfg["bot"]["persona_name"],
        "office_label": cfg["business"]["office_label"],
        "office_full_address": cfg["business"]["office_full_address"],
        "office_maps_url": effective_maps_url,
        "hours_weekdays": cfg["business"]["hours_weekdays"],
        "hours_saturday": cfg["business"]["hours_saturday"],
        **runtime_vars,
    }
    try:
        return cfg["prompt_template"].format(**all_vars)
    except KeyError as e:
        logger.error(f"❌ prompt.md references missing variable: {e}")
        raise


def get_dropdown_map() -> Dict[str, list]:
    return get_brand_config()["dropdown_map"]


def get_tracking_codes() -> Dict[str, str]:
    return get_brand_config()["tracking_codes"]


def get_campaign_types() -> Dict[str, str]:
    return get_brand_config()["campaign_types"]


def get_inventory_path() -> str:
    path = get_brand_config()["inventory_path"]
    if not path:
        raise RuntimeError("No inventory.csv in brand/")
    return path


def get_financing_path() -> str:
    path = get_brand_config()["financing_path"]
    if not path:
        raise RuntimeError("No financing.json in brand/")
    return path