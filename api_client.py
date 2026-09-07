from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

import tz
from config import settings


class PrintBoxAPIError(RuntimeError):
    pass


@dataclass
class Transaction:
    id: str
    date: datetime
    machine: str
    user: str
    telegram_id: str
    amount: float
    status: str
    payment_method: str
    print_type: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transaction":
        return cls(
            id=data["id"],
            date=_parse_dt(data["date"]),
            machine=data["machine"],
            user=data["user"],
            telegram_id=data["telegramId"],
            amount=data["amount"],
            status=data["status"],
            payment_method=data["paymentMethod"],
            print_type=data["printType"],
        )


@dataclass
class Apparat:
    id: int
    name_apparat: str
    address: str
    status: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Apparat":
        # v2 reports health as is_online rather than a status string.
        status = data.get("status")
        if status is None and "is_online" in data:
            status = "online" if data["is_online"] else "offline"
        return cls(
            id=data["id"],
            name_apparat=data["name_apparat"],
            address=data.get("address", ""),
            status=status or "",
        )


@dataclass
class PrinterStatusEvent:
    is_online: bool
    status: str
    error_text: str | None
    created_at: datetime

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PrinterStatusEvent":
        return cls(
            is_online=data["is_online"],
            status=data["status"],
            error_text=data.get("error_text"),
            created_at=_parse_dt(data["created_at"]),
        )


def _parse_dt(value: str) -> datetime:
    # Confirmed empirically: the API returns naive Asia/Almaty wall-clock timestamps
    # ("2026-06-18T14:30:01.093784", no offset/Z). Defensively normalize to that same
    # naive convention (see tz.py) even if a tz-aware value ever shows up.
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(tz.ALMATY).replace(tzinfo=None)
    return parsed


class PrintBoxAPIClient:
    """Async wrapper over the PrintBox admin API (nurtest.space).

    Auth shape (Bearer token from /admin/login) isn't declared in the OpenAPI
    security schemes - this was verified empirically against the live API.
    """

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or settings.printbox_api_base_url).rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=30)
        self._token: str | None = settings.printbox_admin_token or None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _login(self) -> None:
        if settings.printbox_admin_token:
            self._token = settings.printbox_admin_token
            return
        resp = await self._client.post(
            "/v2/admin/login",
            json={
                "username": settings.printbox_admin_login,
                "password": settings.printbox_admin_password,
            },
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self._token is None:
            await self._login()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._token}"
        resp = await self._client.request(method, path, headers=headers, **kwargs)
        if resp.status_code == 401:
            await self._login()
            headers["Authorization"] = f"Bearer {self._token}"
            resp = await self._client.request(method, path, headers=headers, **kwargs)
        if resp.is_error:
            raise PrintBoxAPIError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp

    async def get_transactions(
        self,
        telegram_id: str | None = None,
        transaction_id: str | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> list[Transaction]:
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        if telegram_id is not None:
            params["telegram_id"] = telegram_id
        if transaction_id is not None:
            params["id"] = transaction_id
        resp = await self._request("GET", "/v2/admin/transactions", params=params)
        data = resp.json()
        return [Transaction.from_dict(t) for t in data["transactions"]]

    async def refund_transaction(self, transaction_id: str) -> dict[str, Any]:
        resp = await self._request("POST", f"/v2/admin/transactions/{transaction_id}/refund")
        return resp.json()

    async def get_apparats(self) -> list[Apparat]:
        # /v1/admin/apparats started returning 403 (access tightened server-side);
        # v2 carries the same machines under a doubly-nested "apparats" key.
        resp = await self._request("GET", "/v2/admin/apparats")
        data = resp.json()["apparats"]
        items = data["apparats"] if isinstance(data, dict) else data
        return [Apparat.from_dict(a) for a in items]

    async def get_printer_history(
        self, apparat_id: int, limit: int = 50, offset: int = 0
    ) -> list[PrinterStatusEvent]:
        resp = await self._request(
            "GET",
            f"/v2/admin/printers/{apparat_id}/history",
            params={"limit": limit, "offset": offset},
        )
        data = resp.json()
        return [PrinterStatusEvent.from_dict(e) for e in data["history"]]

    async def get_all_printer_statuses(self) -> list[dict[str, Any]]:
        resp = await self._request("GET", "/v2/admin/printers/status")
        return resp.json()["printers"]

    async def get_printer_alerts(self) -> list[dict[str, Any]]:
        resp = await self._request("GET", "/v2/admin/printers/alerts")
        return resp.json()["alerts"]

    async def get_printer_summary(self) -> dict[str, Any]:
        resp = await self._request("GET", "/v2/admin/printers/summary")
        return resp.json()["summary"]

    async def request_device_logs(self, apparat_id: int, lines: int = 200, log_type: str = "print") -> None:
        await self._request(
            "POST",
            f"/v2/device/{apparat_id}/request-logs",
            json={"lines": lines, "log_type": log_type},
        )

    async def get_device_logs(self, apparat_id: int, log_type: str = "print") -> str:
        resp = await self._request(
            "GET", "/v2/device/logs", params={"pos": apparat_id, "log_type": log_type}
        )
        return resp.json()["content"]

    async def get_telegram_documents(self, telegram_id: str) -> list[dict[str, Any]]:
        # /v1/telegram/documents is broken server-side (always 500s, regardless
        # of telegram_id or whether it has any documents at all - confirmed
        # empirically, not data-dependent). /v2/admin/files/all is what the
        # admin dashboard's own "Документы" page uses and works correctly, so
        # we use that instead and remap field names to the shape the rest of
        # this codebase expects (file_name/created_at/status).
        resp = await self._request(
            "GET",
            "/v2/admin/files/all",
            params={"telegram_id": telegram_id, "per_page": 100},
        )
        data = resp.json()
        return [
            {
                "id": f.get("document_id"),
                "file_name": f.get("original_name"),
                "created_at": f.get("created_at"),
                "status": f.get("status"),
            }
            for f in data.get("files", [])
        ]

