"""
Locations Service — Fuente única de verdad para sucursales y sus links de Maps.

Carga desde Google Sheets (pestaña Sucursales) via CSV export URL.
Fallback: sección `sucursales` de brand.yaml.
Cache con TTL configurable (mismo patrón que InventoryService).

Columnas esperadas en el CSV (case-insensitive):
  sucursal_id | nombre_display | ciudad | estado | maps_url | maps_url_short |
  place_id | direccion | activa | notas
"""

import csv
import logging
import time
from dataclasses import dataclass, field
from io import StringIO
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class Sucursal:
    sucursal_id: str
    nombre_display: str
    ciudad: str = ""
    estado: str = ""
    maps_url: str = ""          # URL canónica de Google Maps (preferida)
    maps_url_short: str = ""    # Link corto maps.app.goo.gl (respaldo)
    place_id: str = ""
    direccion: str = ""
    activa: bool = True
    notas: str = ""

    @property
    def best_maps_url(self) -> str:
        """Devuelve el mejor link disponible: canónico primero, corto de respaldo."""
        return self.maps_url or self.maps_url_short

    def is_usable(self) -> bool:
        return self.activa and bool(self.best_maps_url)


def _parse_bool(value: str) -> bool:
    return (value or "").strip().upper() in ("TRUE", "SI", "SÍ", "YES", "1", "ACTIVA")


def _clean(v) -> str:
    return (str(v) if v is not None else "").strip()


class LocationsService:
    """
    Carga y cachea la tabla de sucursales.

    Prioridad de fuente:
      1. Google Sheets CSV (SUCURSALES_CSV_URL)
      2. brand.yaml → sucursales section (fallback local)
    """

    def __init__(
        self,
        csv_url: Optional[str] = None,
        brand_sucursales: Optional[List[Dict]] = None,
        refresh_seconds: int = 300,
    ):
        self._csv_url = csv_url
        self._brand_sucursales = brand_sucursales or []
        self._refresh_seconds = refresh_seconds
        self._sucursales: Dict[str, Sucursal] = {}
        self._last_load_ts: float = 0

    # ------------------------------------------------------------------
    # Carga
    # ------------------------------------------------------------------

    async def load(self, force: bool = False):
        now = time.time()
        if not force and self._sucursales and (now - self._last_load_ts) < self._refresh_seconds:
            return

        if self._csv_url:
            await self._load_from_sheet()
        else:
            self._load_from_brand_yaml()

        self._last_load_ts = now

    async def ensure_loaded(self):
        await self.load(force=False)

    async def _load_from_sheet(self):
        url = (self._csv_url or "").strip()
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                r = await client.get(url)
            r.raise_for_status()
            rows = list(csv.DictReader(StringIO(r.text)))
            self._sucursales = self._parse_rows(rows, source="sheet")
            logger.info(f"✅ Sucursales cargadas desde Sheet: {len(self._sucursales)} sucursales")
        except Exception as e:
            logger.error(f"⚠️ Error cargando Sucursales desde Sheet: {e} — usando fallback brand.yaml")
            self._load_from_brand_yaml()

    def _load_from_brand_yaml(self):
        rows = self._brand_sucursales
        self._sucursales = self._parse_rows(rows, source="brand.yaml")
        logger.info(f"✅ Sucursales cargadas desde brand.yaml: {len(self._sucursales)} sucursales")

    def _parse_rows(self, rows: List[Dict], source: str) -> Dict[str, Sucursal]:
        result: Dict[str, Sucursal] = {}
        for row in rows:
            cleaned = {_clean(k).lower().replace(" ", "_"): _clean(v) for k, v in (row or {}).items()}
            sid = cleaned.get("sucursal_id", "")
            if not sid:
                continue
            activa_raw = cleaned.get("activa", "true")
            sucursal = Sucursal(
                sucursal_id=sid,
                nombre_display=cleaned.get("nombre_display", sid),
                ciudad=cleaned.get("ciudad", ""),
                estado=cleaned.get("estado", ""),
                maps_url=cleaned.get("maps_url", ""),
                maps_url_short=cleaned.get("maps_url_short", ""),
                place_id=cleaned.get("place_id", ""),
                direccion=cleaned.get("direccion", ""),
                activa=_parse_bool(activa_raw),
                notas=cleaned.get("notas", ""),
            )
            result[sid] = sucursal
        return result

    # ------------------------------------------------------------------
    # Consulta
    # ------------------------------------------------------------------

    def get(self, sucursal_id: str) -> Optional[Sucursal]:
        """Devuelve la sucursal por ID, o None si no existe / está inactiva."""
        s = self._sucursales.get((sucursal_id or "").strip())
        if s is None:
            return None
        if not s.activa:
            logger.warning(f"📍 Sucursal inactiva solicitada: {sucursal_id}")
            return None
        return s

    def all_active(self) -> List[Sucursal]:
        return [s for s in self._sucursales.values() if s.activa]

    def __len__(self) -> int:
        return len(self._sucursales)

    # ------------------------------------------------------------------
    # Validación de links (startup + monitoreo)
    # ------------------------------------------------------------------

    async def validate_maps_urls(self) -> List[str]:
        """
        Verifica que cada maps_url responda correctamente.
        Devuelve lista de errores (vacía = todo OK).
        """
        errors: List[str] = []
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            for s in self._sucursales.values():
                for campo, url in [("maps_url", s.maps_url), ("maps_url_short", s.maps_url_short)]:
                    if not url:
                        continue
                    try:
                        r = await client.head(url)
                        if r.status_code >= 400:
                            errors.append(
                                f"❌ {s.sucursal_id}.{campo} → HTTP {r.status_code}: {url}"
                            )
                            logger.warning(
                                f"📍 Link roto detectado: {s.sucursal_id}.{campo} "
                                f"HTTP {r.status_code} → {url}"
                            )
                    except Exception as e:
                        errors.append(f"❌ {s.sucursal_id}.{campo} → Error: {e}: {url}")
                        logger.warning(f"📍 Link inalcanzable: {s.sucursal_id}.{campo} → {e}")
        return errors
