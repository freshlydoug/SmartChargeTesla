"""myenergi Zappi charging-status client.

Checks whether the Zappi EV charger is actively drawing power from the
grid or solar, so the dispatch action loop can decide whether to hold
the Powerwall in BACKUP mode.

Uses the pymyenergi library with hub_serial as username and api_key as
the Digest Auth password. These are found in the myenergi app under:
  Settings → myenergi hub → Advanced

Plug status codes:
  A         = disconnected
  B1 / B2   = connected, not charging
  C1 / C2   = actively charging
  Charging  = actively charging (alternate string from some firmwares)
"""

import asyncio
from typing import Optional

try:
    from pymyenergi.connection import Connection
    from pymyenergi.client import MyenergiClient
    from pymyenergi import ZAPPI
    _HAS_PYMYENERGI = True
except ImportError:
    _HAS_PYMYENERGI = False

_CHARGING_STATUSES = {"C1", "C2", "Charging"}


class ZappiStatusAPI:
    """Minimal myenergi client for Zappi plug-status polling."""

    def __init__(self, hub_serial: str, api_key: str):
        if not _HAS_PYMYENERGI:
            raise ImportError("pymyenergi not installed — run: pip install pymyenergi")
        self.hub_serial = hub_serial
        self.api_key = api_key

    def _make_client(self) -> "MyenergiClient":
        conn = Connection(username=self.hub_serial, password=self.api_key)
        return MyenergiClient(conn)

    async def _get_plug_statuses(self) -> list[str]:
        client = self._make_client()
        await client.refresh()
        zappis = client.get_devices_sync(ZAPPI)
        return [getattr(z, "plug_status", "A") for z in zappis]

    async def is_charging_async(self) -> bool:
        """True if any Zappi is actively charging. Safe to call from asyncio context."""
        statuses = await self._get_plug_statuses()
        return any(s in _CHARGING_STATUSES for s in statuses)

    def is_charging(self) -> bool:
        """Synchronous wrapper — do not call from inside a running event loop."""
        return asyncio.run(self.is_charging_async())
