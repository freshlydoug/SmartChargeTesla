"""EON Next Kraken GraphQL API client — dispatch windows only.

Fetches planned SmartFlex and completed EV dispatch windows from the
EON Next Kraken platform (Drive Smart / Intelligent Octopus equivalent).

Authentication uses the standard Kraken GraphQL endpoint. The account
number is the import electricity account (format: A-XXXXXXXX).

EON Next Kraken API endpoint: https://api.eonnext-kraken.energy/v1/graphql/
This API is unofficial/undocumented. It is the same backend used by the
EON Next mobile app and the Kraken portal.
"""

import requests
from typing import Optional

GRAPHQL_URL = "https://api.eonnext-kraken.energy/v1/graphql/"

_LOGIN_MUTATION = """
mutation krakenTokenAuthentication($email: String!, $password: String!) {
  obtainKrakenToken(input: {email: $email, password: $password}) {
    token
  }
}
"""

_KRAKENFLEX_DEVICE_QUERY = """
query getKrakenflexDevice($accountNumber: String!) {
  registeredKrakenflexDevice(accountNumber: $accountNumber) {
    krakenflexDeviceId vehicleMake vehicleModel
    vehicleBatterySizeInKwh chargePointMake chargePointModel status
  }
}
"""

_PLANNED_DISPATCHES_QUERY = """
query getPlannedDispatches($accountNumber: String!) {
  plannedDispatches(accountNumber: $accountNumber) {
    start end delta
    meta { source location }
  }
}
"""

_FLEX_PLANNED_DISPATCHES_QUERY = """
query getFlexPlannedDispatches($deviceId: String!) {
  flexPlannedDispatches(deviceId: $deviceId) {
    start end type energyAddedKwh
  }
}
"""

_COMPLETED_DISPATCHES_QUERY = """
query getCompletedDispatches($accountNumber: String!) {
  completedDispatches(accountNumber: $accountNumber) {
    start end delta
    meta { source location }
  }
}
"""


class KrakenDispatchAPI:
    """Kraken GraphQL client scoped to EV dispatch queries."""

    def __init__(self, email: str, password: str, account_number: str):
        self.email = email
        self.password = password
        self.account_number = account_number
        self._token: Optional[str] = None

    def _gql(self, query: str, variables: dict = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"JWT {self._token}"
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        resp = requests.post(GRAPHQL_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise ValueError(f"GraphQL errors: {data['errors']}")
        return data

    def authenticate(self):
        data = self._gql(_LOGIN_MUTATION, {"email": self.email, "password": self.password})
        self._token = data["data"]["obtainKrakenToken"]["token"]

    def get_device_id(self) -> Optional[str]:
        """Return the KrakenFlex device ID for the account, or None if not enrolled."""
        data = self._gql(_KRAKENFLEX_DEVICE_QUERY, {"accountNumber": self.account_number})
        device = data["data"].get("registeredKrakenflexDevice")
        return device["krakenflexDeviceId"] if device else None

    def get_dispatches(self, device_id: Optional[str] = None) -> dict:
        """Fetch planned and completed dispatch windows.

        Returns dict with keys:
          planned   — upcoming off-schedule cheap-rate windows
          flex      — SmartFlex planned windows (requires device_id)
          completed — recently completed dispatch windows

        Each entry: {start, end, delta_kwh, type, source, location}
        """
        if not self._token:
            self.authenticate()

        result: dict = {"planned": [], "flex": [], "completed": []}

        data = self._gql(_PLANNED_DISPATCHES_QUERY, {"accountNumber": self.account_number})
        for d in data["data"].get("plannedDispatches", []):
            result["planned"].append({
                "start": d["start"],
                "end": d["end"],
                "delta_kwh": float(d["delta"]) if d.get("delta") else None,
                "type": None,
                "source": (d.get("meta") or {}).get("source"),
                "location": (d.get("meta") or {}).get("location"),
            })

        if device_id:
            data = self._gql(_FLEX_PLANNED_DISPATCHES_QUERY, {"deviceId": device_id})
            for d in data["data"].get("flexPlannedDispatches", []):
                result["flex"].append({
                    "start": d["start"],
                    "end": d["end"],
                    "delta_kwh": float(d["energyAddedKwh"]) if d.get("energyAddedKwh") else None,
                    "type": d.get("type"),
                    "source": None,
                    "location": None,
                })

        data = self._gql(_COMPLETED_DISPATCHES_QUERY, {"accountNumber": self.account_number})
        for d in data["data"].get("completedDispatches", []):
            result["completed"].append({
                "start": d["start"],
                "end": d["end"],
                "delta_kwh": float(d["delta"]) if d.get("delta") else None,
                "type": None,
                "source": (d.get("meta") or {}).get("source"),
                "location": (d.get("meta") or {}).get("location"),
            })

        return result
