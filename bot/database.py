from __future__ import annotations

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

    async def get_state(self, user_id: int) -> str | None:
        user = await self.get_user(user_id)
        return user.get("state") if user else None

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
