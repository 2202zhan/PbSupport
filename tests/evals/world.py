"""A PrintBox that behaves however a scenario needs it to.

Everything the agent can learn about the world comes through this object, so a
scenario is defined by the state it sets here plus what the user types. Nothing
reaches the real API - including, and especially, the refund endpoint: an
attempt to call it fails the run rather than being quietly ignored.
"""

from dataclasses import dataclass, field
from datetime import timedelta

import tz
from api_client import Apparat, Transaction


@dataclass
class World:
    """Apparats keyed by name, with their readings."""

    toner: int = 60
    pages: int = 300
    error_text: str | None = None
    online: bool = True
    # Payments this telegram_id has made, as (minutes ago, amount).
    payments: list[tuple[int, float]] = field(default_factory=list)
    printed_ok: bool | None = None
    download_error: bool | None = None
    documents: int = 1
    refund_attempts: list[str] = field(default_factory=list)

    def transactions(self) -> list[Transaction]:
        return [
            Transaction(
                id=f"tx-{i}",
                date=tz.now() - timedelta(minutes=ago),
                machine="Аппарат №3 3️⃣",
                user="@zhan",
                telegram_id="884013433",
                amount=amount,
                status="completed",
                payment_method="kaspi",
                print_type="bw",
            )
            for i, (ago, amount) in enumerate(self.payments)
        ]


class FakeApi:
    def __init__(self, world: World) -> None:
        self.world = world
        self.calls: list[str] = []

    async def get_apparats(self):
        self.calls.append("get_apparats")
        return [
            Apparat(id=3, name_apparat="Аппарат №3 3️⃣", address="Первый корпус",
                    status="online" if self.world.online else "offline",
                    pages_left=self.world.pages),
            Apparat(id=1, name_apparat="Аппарат №1", address="Главный корпус, 1 этаж",
                    status="online", pages_left=400),
            Apparat(id=2, name_apparat="Аппарат №2", address="Второй корпус", status="online",
                    pages_left=350),
        ]

    async def get_all_printer_statuses(self):
        self.calls.append("get_all_printer_statuses")
        return [
            {"apparat_id": 3, "is_online": self.world.online,
             "toner": {"black": self.world.toner}, "error_text": self.world.error_text},
            {"apparat_id": 1, "is_online": True, "toner": {"black": 74}, "error_text": None},
            {"apparat_id": 2, "is_online": True, "toner": {"black": 66}, "error_text": None},
        ]

    async def get_transactions(self, telegram_id=None, transaction_id=None, page=1, per_page=50):
        self.calls.append("get_transactions")
        return self.world.transactions()

    async def get_printer_history(self, apparat_id, limit=50, offset=0):
        self.calls.append("get_printer_history")
        return []

    async def get_printer_alerts(self):
        return []

    async def get_printer_summary(self):
        return {}

    async def get_telegram_documents(self, telegram_id):
        self.calls.append("get_telegram_documents")
        return [{"id": "d1", "file_name": "a.pdf", "created_at": tz.now().isoformat(),
                 "status": "completed"}] * self.world.documents

    async def request_device_logs(self, apparat_id, lines=1000, log_type="print"):
        return None

    async def get_device_logs(self, apparat_id, log_type="print"):
        self.calls.append("get_device_logs")
        if self.world.download_error:
            return "ERROR downloading file a.pdf: timeout"
        if self.world.printed_ok:
            return "Print process completed successfully"
        return ""

    async def refund_transaction(self, transaction_id: str):
        # The one thing that must never happen from inside a turn.
        self.world.refund_attempts.append(transaction_id)
        raise AssertionError("агент попытался вернуть деньги сам")
