"""One-off manual smoke check against the real Telegram/PrintBox/OpenAI credentials
in .env - NOT part of the automated test suite (hits live services). Run with:
    python scripts/smoke_check.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Bot

from api_client import PrintBoxAPIClient
from config import settings


async def check_telegram() -> None:
    bot = Bot(token=settings.support_bot_token)
    me = await bot.get_me()
    print(f"[telegram] OK: bot @{me.username} (id={me.id})")
    await bot.session.close()


async def check_printbox() -> None:
    api = PrintBoxAPIClient()
    try:
        apparats = await api.get_apparats()
        print("[printbox] login + get_apparats OK")
        print(f"[printbox] get_apparats OK: {len(apparats)} apparats")
        for a in apparats[:5]:
            print(f"    id={a.id} name={a.name_apparat!r} status={a.status!r}")
        statuses = await api.get_all_printer_statuses()
        print(f"[printbox] get_all_printer_statuses OK: {len(statuses)} entries")
        if statuses:
            print(f"    sample keys: {sorted(statuses[0].keys())}")
        txs = await api.get_transactions(page=1, per_page=5)
        print(f"[printbox] get_transactions OK: {len(txs)} transactions (page 1)")
        for t in txs[:3]:
            print(f"    id={t.id} machine={t.machine!r} amount={t.amount} status={t.status!r} date={t.date}")
    finally:
        await api.aclose()


async def check_openai() -> None:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    resp = await client.chat.completions.create(
        model=settings.openai_model,
        messages=[{"role": "user", "content": "Ответь одним словом: ок"}],
        max_tokens=5,
    )
    print(f"[openai] OK: model={settings.openai_model} reply={resp.choices[0].message.content!r}")


async def main() -> None:
    for name, coro in [
        ("telegram", check_telegram()),
        ("printbox", check_printbox()),
        ("openai", check_openai()),
    ]:
        try:
            await coro
        except Exception as exc:
            print(f"[{name}] FAILED: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
