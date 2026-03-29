from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase


class Database:
    def __init__(self, mongo_uri: str, db_name: str) -> None:
        self.client = AsyncIOMotorClient(mongo_uri)
        self.db: AsyncIOMotorDatabase = self.client[db_name]
        self.users = self.db.users

    async def setup(self) -> None:
        await self.users.create_index("user_id", unique=True)

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        return await self.users.find_one({"user_id": user_id})

    async def register_user(self, user: Any) -> None:
        now = datetime.now(timezone.utc)
        full_name = " ".join(
            part for part in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if part
        ).strip()
        await self.users.update_one(
            {"user_id": user.id},
            {
                "$set": {
                    "user_id": user.id,
                    "username": getattr(user, "username", None),
                    "first_name": getattr(user, "first_name", None),
                    "last_name": getattr(user, "last_name", None),
                    "full_name": full_name or getattr(user, "first_name", None),
                    "last_seen_at": now,
                },
                "$setOnInsert": {"started_at": now},
            },
            upsert=True,
        )

    async def iter_user_ids(self):
        cursor = self.users.find({"user_id": {"$exists": True}}, {"user_id": 1, "_id": 0})
        async for document in cursor:
            user_id = document.get("user_id")
            if isinstance(user_id, int):
                yield user_id

    async def get_state(self, user_id: int) -> str | None:
        user = await self.get_user(user_id)
        return user.get("state") if user else None

    async def save_var_keys(self, user_id: int, app_name: str, keys: list[str]) -> None:
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {f"ui_var_keys.{app_name}": keys}},
            upsert=True,
        )

    async def get_var_keys(self, user_id: int, app_name: str) -> list[str]:
        user = await self.get_user(user_id)
        if not user:
            return []
        return user.get("ui_var_keys", {}).get(app_name, [])

    async def set_state(self, user_id: int, state: str | None) -> None:
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {"state": state}},
            upsert=True,
        )

    async def save_api_key(
        self,
        user_id: int,
        api_key: str,
        account: dict[str, Any],
    ) -> None:
        await self.users.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "user_id": user_id,
                    "heroku_api_key": api_key,
                    "account": account,
                    "state": None,
                }
            },
            upsert=True,
        )

    async def clear_api_key(self, user_id: int) -> None:
        await self.users.update_one(
            {"user_id": user_id},
            {
                "$unset": {
                    "heroku_api_key": "",
                    "account": "",
                    "app_backups": "",
                },
                "$set": {"state": None},
            },
            upsert=True,
        )

    async def save_formation_backup(
        self,
        user_id: int,
        app_name: str,
        quantities: dict[str, int],
    ) -> None:
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {f"app_backups.{app_name}": quantities}},
            upsert=True,
        )

    async def get_formation_backup(
        self,
        user_id: int,
        app_name: str,
    ) -> dict[str, int] | None:
        user = await self.get_user(user_id)
        if not user:
            return None
        return user.get("app_backups", {}).get(app_name)

    async def save_vps_server(
        self,
        user_id: int,
        server_id: str,
        server_data: dict[str, Any],
    ) -> None:
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {f"vps_servers.{server_id}": server_data}},
            upsert=True,
        )

    async def list_vps_servers(self, user_id: int) -> list[dict[str, Any]]:
        user = await self.get_user(user_id)
        if not user:
            return []
        servers = user.get("vps_servers", {})
        if not isinstance(servers, dict):
            return []
        result: list[dict[str, Any]] = []
        for server_id, server_data in servers.items():
            if isinstance(server_data, dict):
                result.append({"id": server_id, **server_data})
        return sorted(result, key=lambda item: str(item.get("name", item["id"])).lower())

    async def get_vps_server(self, user_id: int, server_id: str) -> dict[str, Any] | None:
        user = await self.get_user(user_id)
        if not user:
            return None
        server = user.get("vps_servers", {}).get(server_id)
        if not isinstance(server, dict):
            return None
        return {"id": server_id, **server}

    async def delete_vps_server(self, user_id: int, server_id: str) -> None:
        await self.users.update_one(
            {"user_id": user_id},
            {
                "$unset": {
                    f"vps_servers.{server_id}": "",
                    f"vps_bots.{server_id}": "",
                    f"vps_scan_results.{server_id}": "",
                }
            },
            upsert=True,
        )

    async def save_vps_bot(
        self,
        user_id: int,
        server_id: str,
        bot_id: str,
        bot_data: dict[str, Any],
    ) -> None:
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {f"vps_bots.{server_id}.{bot_id}": bot_data}},
            upsert=True,
        )

    async def list_vps_bots(self, user_id: int, server_id: str) -> list[dict[str, Any]]:
        user = await self.get_user(user_id)
        if not user:
            return []
        bots = user.get("vps_bots", {}).get(server_id, {})
        if not isinstance(bots, dict):
            return []
        result: list[dict[str, Any]] = []
        for bot_id, bot_data in bots.items():
            if isinstance(bot_data, dict):
                result.append({"id": bot_id, **bot_data})
        return sorted(result, key=lambda item: str(item.get("label", item["id"])).lower())

    async def get_vps_bot(self, user_id: int, server_id: str, bot_id: str) -> dict[str, Any] | None:
        user = await self.get_user(user_id)
        if not user:
            return None
        bot = user.get("vps_bots", {}).get(server_id, {}).get(bot_id)
        if not isinstance(bot, dict):
            return None
        return {"id": bot_id, **bot}

    async def delete_vps_bot(self, user_id: int, server_id: str, bot_id: str) -> None:
        await self.users.update_one(
            {"user_id": user_id},
            {"$unset": {f"vps_bots.{server_id}.{bot_id}": ""}},
            upsert=True,
        )

    async def save_vps_scan_results(
        self,
        user_id: int,
        server_id: str,
        scan_type: str,
        items: list[str],
    ) -> None:
        await self.users.update_one(
            {"user_id": user_id},
            {"$set": {f"vps_scan_results.{server_id}.{scan_type}": items}},
            upsert=True,
        )

    async def get_vps_scan_results(self, user_id: int, server_id: str, scan_type: str) -> list[str]:
        user = await self.get_user(user_id)
        if not user:
            return []
        scan_results = user.get("vps_scan_results", {}).get(server_id, {}).get(scan_type, [])
        if not isinstance(scan_results, list):
            return []
        return [str(item) for item in scan_results]
