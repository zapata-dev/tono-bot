"""
Campaign Service — Lee campañas activas desde Google Sheets.

El Sheet tiene 5 columnas:
  Activa | Tracking ID | Keywords | Campaña | Instrucciones

El bot lee el Sheet periódicamente, filtra las activas,
y genera bloques de texto para inyectar en el System Prompt.
"""

import csv
import logging
import time
from io import StringIO
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# Columnas esperadas en el CSV (case-insensitive matching)
_EXPECTED_COLUMNS = {"activa", "tracking id", "keywords", "campaña", "campana", "instrucciones"}


def _extract_form_url(instructions: str) -> tuple:
    """
    Extracts optional FORM_URL and BASES_PDF_URL lines from instructions text.
    Returns (form_url, bases_pdf_url, cleaned_instructions).

    Line formats:
      FORM_URL: https://...
      BASES_PDF_URL: https://...
    Both lines are stripped from instructions so they don't reach the LLM prompt.
    """
    import re as _re
    form_url = ""
    bases_pdf_url = ""
    lines = instructions.splitlines()
    kept = []
    for line in lines:
        m = _re.match(r'^\s*FORM_URL\s*:\s*(https?://\S+)\s*$', line, _re.IGNORECASE)
        if m:
            form_url = m.group(1).strip()
            continue
        m2 = _re.match(r'^\s*BASES(?:_PDF)?_URL\s*:\s*(https?://\S+)\s*$', line, _re.IGNORECASE)
        if m2:
            bases_pdf_url = m2.group(1).strip()
            continue
        kept.append(line)
    return form_url, bases_pdf_url, "\n".join(kept).strip()


class Campaign:
    """Representa una campaña activa del Sheet."""

    def __init__(self, row: Dict[str, str]):
        self.active = (row.get("Activa", "") or "").strip().upper() in ("SI", "SÍ", "YES", "TRUE", "1")
        self.tracking_id = (row.get("Tracking ID", "") or "").strip()
        self.keywords = [
            k.strip().lower()
            for k in (row.get("Keywords", "") or "").split(",")
            if k.strip()
        ]
        self.name = (row.get("Campaña", row.get("Campana", "")) or "").strip()
        raw_instructions = (row.get("Instrucciones", "") or "").strip()
        # Extract optional FORM_URL and BASES_PDF_URL lines.
        # Stripped from instructions so the LLM doesn't get confused.
        self.form_url, self.bases_pdf_url, self.instructions = _extract_form_url(raw_instructions)

    def is_valid(self) -> bool:
        """Una campaña es válida si está activa y tiene instrucciones."""
        return self.active and bool(self.instructions)


def _normalize_columns(fieldnames: List[str]) -> Dict[str, str]:
    """
    Mapea columnas del CSV a nombres canónicos, tolerando variaciones.
    Retorna dict: {nombre_original: nombre_canónico}
    """
    canonical_map = {
        "activa": "Activa",
        "active": "Activa",
        "tracking id": "Tracking ID",
        "trackingid": "Tracking ID",
        "tracking_id": "Tracking ID",
        "keywords": "Keywords",
        "palabras clave": "Keywords",
        "campaña": "Campaña",
        "campana": "Campaña",
        "campaign": "Campaña",
        "nombre": "Campaña",
        "instrucciones": "Instrucciones",
        "instructions": "Instrucciones",
        "reglas": "Instrucciones",
    }
    mapping = {}
    for field in fieldnames:
        if not field:
            continue
        key = field.strip().lower()
        if key in canonical_map:
            mapping[field] = canonical_map[key]
        else:
            mapping[field] = field.strip()
    return mapping


class CampaignService:
    """Lee y cachea campañas activas desde Google Sheets CSV."""

    def __init__(self, csv_url: Optional[str] = None, refresh_seconds: int = 300):
        self.csv_url = (csv_url or "").strip() or None
        self.refresh_seconds = refresh_seconds
        self.campaigns: List[Campaign] = []
        self._last_load_ts: float = 0

    async def load(self, force: bool = False) -> None:
        """Carga campañas desde el Sheet CSV."""
        if not self.csv_url:
            self.campaigns = []
            return

        now = time.time()
        if not force and self.campaigns is not None and (now - self._last_load_ts) < self.refresh_seconds:
            return

        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                r = await client.get(self.csv_url)
            r.raise_for_status()

            text = r.text.strip()
            if not text:
                logger.warning("⚠️ CSV de campañas vacío")
                self.campaigns = []
                self._last_load_ts = now
                return

            reader = csv.DictReader(StringIO(text))
            fieldnames = reader.fieldnames or []

            # Validar que las columnas esperadas existan
            col_map = _normalize_columns(fieldnames)
            found_canonical = set(col_map.values())
            missing = {"Activa", "Instrucciones"} - found_canonical
            if missing:
                logger.error(f"❌ Columnas faltantes en CSV de campañas: {missing}. Columnas encontradas: {fieldnames}")
                return  # No sobrescribir campañas anteriores

            # Parsear filas con nombres normalizados
            loaded = []
            seen_tracking_ids = set()
            total_rows = 0
            skipped_empty = 0

            for row in reader:
                total_rows += 1
                # Renombrar columnas a nombres canónicos
                normalized = {}
                for orig_key, value in row.items():
                    canonical_key = col_map.get(orig_key, orig_key)
                    normalized[canonical_key] = (str(value) if value else "").strip()

                # Saltar filas completamente vacías
                if not any(normalized.values()):
                    skipped_empty += 1
                    continue

                campaign = Campaign(normalized)
                if not campaign.is_valid():
                    continue

                # Detectar tracking IDs duplicados
                if campaign.tracking_id:
                    tid_upper = campaign.tracking_id.upper()
                    if tid_upper in seen_tracking_ids:
                        logger.warning(f"⚠️ Tracking ID duplicado ignorado: {campaign.tracking_id} (campaña: {campaign.name})")
                        continue
                    seen_tracking_ids.add(tid_upper)

                loaded.append(campaign)

            self.campaigns = loaded
            self._last_load_ts = now

            log_parts = [f"{len(loaded)} activas de {total_rows} filas"]
            if skipped_empty:
                log_parts.append(f"{skipped_empty} vacías")
            logger.info(f"📢 Campañas cargadas: {', '.join(log_parts)}")

        except httpx.HTTPStatusError as e:
            logger.error(f"⚠️ Error HTTP cargando campañas: {e.response.status_code}")
        except Exception as e:
            logger.error(f"⚠️ Error cargando campañas: {e}")
            # Mantener campañas anteriores en caso de error de red

    async def ensure_loaded(self) -> None:
        """Asegura que las campañas estén cargadas (usa cache)."""
        await self.load(force=False)

    def get_active_campaigns(self) -> List[Campaign]:
        """Retorna solo campañas activas y válidas."""
        return [c for c in self.campaigns if c.is_valid()]

    def find_campaign_by_tracking_id(self, tracking_id: str) -> Optional[Campaign]:
        """Busca campaña por tracking ID."""
        if not tracking_id:
            return None
        tid = tracking_id.strip().upper()
        for c in self.get_active_campaigns():
            if c.tracking_id.upper() == tid:
                return c
        return None

    def find_campaign_by_model_code(self, model_code: str, campaign_type: str = "") -> Optional[Campaign]:
        """Fallback: busca campaña cuyo tracking ID comparta el mismo código de modelo y tipo.

        Ejemplo: model_code="CA", campaign_type="SU" matchea campaña con tracking_id="CA-SU1".
        Evita que un anuncio regular (CA-A) active una campaña especial (CA-SU).
        """
        if not model_code:
            return None
        prefix = model_code.strip().upper()
        ctype = campaign_type.strip().upper() if campaign_type else ""
        for c in self.get_active_campaigns():
            if c.tracking_id:
                tid = c.tracking_id.upper()
                if ctype:
                    # Debe coincidir el modelo Y el tipo de campaña (ej: "CA-SU")
                    if tid.startswith(f"{prefix}-{ctype}"):
                        return c
                else:
                    # Comportamiento legacy (solo modelo) si no se pasa el tipo
                    if tid.startswith(prefix + "-"):
                        return c
        return None

    def find_campaign_by_keywords(self, message: str) -> Optional[Campaign]:
        """Busca campaña que coincida con keywords en el mensaje."""
        if not message:
            return None
        msg_lower = message.lower()
        for c in self.get_active_campaigns():
            if c.keywords and any(kw in msg_lower for kw in c.keywords):
                return c
        return None

    def build_campaigns_prompt_block(self) -> str:
        """
        Genera el bloque de texto de campañas para inyectar en el System Prompt.
        Cada campaña activa se convierte en una regla temporal.
        """
        active = self.get_active_campaigns()
        if not active:
            return ""

        blocks = []
        for c in active:
            block = (
                f'*** CAMPAÑA ACTIVA: "{c.name}" ***\n'
            )
            if c.tracking_id:
                block += f"TRACKING ID: {c.tracking_id}\n"
            if c.keywords:
                block += f"KEYWORDS DE DETECCIÓN: {', '.join(c.keywords)}\n"
            block += (
                f"\nINSTRUCCIONES:\n"
                f"{c.instructions}\n"
                f"*** FIN CAMPAÑA: {c.name} ***"
            )
            blocks.append(block)

        header = (
            "=== CAMPAÑAS ACTIVAS ===\n"
            "Las siguientes campañas están ACTIVAS. Si un cliente llega con un Tracking ID "
            "o menciona EXPLÍCITAMENTE las keywords de alguna campaña, puedes usar las instrucciones de esa campaña.\n"
            "IMPORTANTE: Si el cliente es ambiguo o no ha confirmado interés en la unidad de la campaña, "
            "NO sueltes precio, ubicación ni condiciones de la campaña. Primero confirma que se refiere a esa unidad.\n"
            "Si el cliente NO está relacionado con ninguna campaña, ignora este bloque completamente.\n\n"
        )

        return header + "\n\n".join(blocks) + "\n=== FIN CAMPAÑAS ACTIVAS ==="
