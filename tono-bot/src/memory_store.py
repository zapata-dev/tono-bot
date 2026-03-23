import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from supabase import create_client, Client

logger = logging.getLogger("BotTractos")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")


class MemoryStore:
    """Session persistence backed by Supabase (PostgreSQL).

    Drop-in replacement for the previous SQLite-based store.
    Same public interface: init(), get(), upsert(), close().
    """

    def __init__(self, url: str = SUPABASE_URL, key: str = SUPABASE_KEY):
        self._url = url
        self._key = key
        self._client: Optional[Client] = None

    async def init(self):
        if not self._url or not self._key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_KEY must be set. "
                "See CLAUDE.md for setup instructions."
            )
        self._client = create_client(self._url, self._key)
        # Verify connectivity with a lightweight query
        self._client.table("sessions").select("phone").limit(1).execute()
        logger.info("✅ Supabase MemoryStore connected.")

    async def get(self, phone: str) -> Optional[Dict[str, Any]]:
        resp = (
            self._client
            .table("sessions")
            .select("phone, state, context_json")
            .eq("phone", phone)
            .limit(1)
            .execute()
        )
        if not resp.data:
            return None
        row = resp.data[0]
        ctx = row.get("context_json") or {}
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except Exception:
                ctx = {}
        return {
            "phone": row["phone"],
            "state": row["state"],
            "context_json": json.dumps(ctx, ensure_ascii=False) if isinstance(ctx, dict) else ctx,
            "context": ctx,
        }

    async def upsert(self, phone: str, state: str, context: Dict[str, Any]):
        now = datetime.now(timezone.utc).isoformat()
        self._client.table("sessions").upsert(
            {
                "phone": phone,
                "state": state,
                "context_json": context,  # Supabase JSONB accepts dicts directly
                "updated_at": now,
            },
            on_conflict="phone",
        ).execute()

    async def close(self):
        # supabase-py uses httpx internally; no explicit close needed
        self._client = None
