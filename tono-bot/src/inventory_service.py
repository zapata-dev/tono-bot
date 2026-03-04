import csv
import os
import time
import httpx
from io import StringIO

def _clean_price(value):
    if value is None:
        return ""
    s = str(value).strip()
    s2 = s.replace("$", "").replace(",", "").strip()
    return s2 if s2 else s

def _clean_cell(v):
    # convierte cualquier cosa (incluidas listas) a texto
    return (str(v) if v is not None else "").strip()

class InventoryService:
    def __init__(self, local_path: str, sheet_csv_url: str | None = None, refresh_seconds: int = 300):
        self.local_path = local_path
        self.sheet_csv_url = sheet_csv_url
        self.refresh_seconds = refresh_seconds
        self.items = []
        self._last_load_ts = 0

    async def load(self, force: bool = False):
        now = time.time()
        if not force and self.items and (now - self._last_load_ts) < self.refresh_seconds:
            return

        rows = []
        if self.sheet_csv_url:
            url = (self.sheet_csv_url or "").strip()
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                r = await client.get(url)
            r.raise_for_status()
            content = r.text

            f = StringIO(content)
            reader = csv.DictReader(f)
            rows = list(reader)
        else:
            if not os.path.exists(self.local_path):
                self.items = []
                return
            with open(self.local_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)

        normalized = []
        for row in rows:
            # limpia headers/valores (a prueba de listas)
            row = { _clean_cell(k): _clean_cell(v) for k, v in (row or {}).items() }

            status = (row.get("status", "") or "").strip().lower()
            if status and status not in ["disponible", "available", "1", "si", "sí", "yes"]:
                continue

            # Filtrar unidades agotadas (Cantidad = 0)
            cantidad_raw = (row.get("Cantidad", "") or "").strip()
            if cantidad_raw:
                try:
                    if int(cantidad_raw) <= 0:
                        continue
                except (ValueError, TypeError):
                    pass

            item = {
                "Marca": row.get("Marca", ""),
                "Modelo": row.get("Modelo", ""),
                "Año": row.get("Año", row.get("Anio", "")),
                "Color": row.get("Color", ""),
                "segmento": row.get("segmento", ""),
                "Precio": _clean_price(row.get("Precio", row.get("Precio Distribuidor", row.get(" Precio Distribuidor", "")))),
                "moneda": row.get("moneda", ""),
                "iva_incluido": row.get("iva_incluido", ""),
                "garantia_texto": row.get("garantia_texto", ""),
                "ubicacion": row.get("ubicacion", ""),
                "ubicacion_link": row.get("ubicacion_link", ""),
                "descripcion_corta": row.get("descripcion_corta", ""),
                "Financiamiento": row.get("Financiamiento", ""),
                "Tipo de financiamiento": row.get("Tipo de financiamiento", ""),
                "Banco": row.get("Banco", ""),
                "photos": row.get("photos", ""),
                "CAPACIDAD DE CARGA": row.get("CAPACIDAD DE CARGA", ""),
                "LLANTAS": row.get("LLANTAS", ""),
                "COMBUSTIBLE": row.get("COMBUSTIBLE", ""),
                "MOTOR": row.get("MOTOR", ""),
                "Cantidad": row.get("Cantidad", "1"),
                "Colores": row.get("Colores", ""),
                "TipoCabina": row.get("TipoCabina", ""),
                "Asientos": row.get("Asientos", ""),
                "Transmision": row.get("Transmisión", row.get("Transmision", "")),
                "Paso": row.get("Paso", ""),
                "Rodada": row.get("Rodada", ""),
                "EjeDelantera": row.get("Eje Delantera", row.get("EjeDelantera", "")),
                "EjeTrasera": row.get("Eje Trasera", row.get("EjeTrasera", "")),
                "Dormitorio": row.get("Dormitorio", ""),
                "Traccion": row.get("Traccion", row.get("Tracción", row.get("traccion", ""))),
            }
            normalized.append(item)

        self.items = normalized
        self._last_load_ts = now

    async def ensure_loaded(self):
        await self.load(force=False)

