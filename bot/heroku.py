from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp


class HerokuAPIError(Exception):
    """Raised when the Heroku API returns an error."""


@dataclass(slots=True)
class FormationBackup:
    quantities: dict[str, int]


class HerokuClient:
    BASE_URL = "https://api.heroku.com"

    def __init__(self, api_key: str, session: aiohttp.ClientSession) -> None:
        self.api_key = api_key
        self.session = session

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.heroku+json; version=3",
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_data: dict[str, Any] | list[dict[str, Any]] | None = None,
        allow_404: bool = False,
    ) -> Any:
        url = f"{self.BASE_URL}{path}"
        async with self.session.request(
            method,
            url,
            headers=self.headers,
            json=json_data,
        ) as response:
            if allow_404 and response.status == 404:
                return None
            if response.status >= 400:
                try:
                    payload = await response.json()
                except aiohttp.ContentTypeError:
                    payload = {"message": await response.text()}
                message = payload.get("message") or payload.get("id") or "Heroku API error"
                raise HerokuAPIError(message)

            if response.content_type == "application/json":
                return await response.json()
            return await response.text()

    async def validate_token(self) -> dict[str, Any]:
        return await self._request("GET", "/account")

    async def list_apps(self) -> list[dict[str, Any]]:
        apps = await self._request("GET", "/apps")
        return sorted(apps, key=lambda app: app["name"].lower())

    async def get_app(self, app_name: str) -> dict[str, Any]:
        return await self._request("GET", f"/apps/{app_name}")

    async def get_formation(self, app_name: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"/apps/{app_name}/formation")

    async def get_config_vars(self, app_name: str) -> dict[str, str]:
        return await self._request("GET", f"/apps/{app_name}/config-vars")

    async def update_config_vars(
        self,
        app_name: str,
        updates: dict[str, str | None],
    ) -> dict[str, str]:
        return await self._request(
            "PATCH",
            f"/apps/{app_name}/config-vars",
            json_data=updates,
        )

    async def list_releases(self, app_name: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"/apps/{app_name}/releases")

    async def redeploy_app(self, app_name: str) -> dict[str, Any]:
        releases = await self.list_releases(app_name)
        current_release = next((release for release in releases if release.get("current")), None)
        if not current_release and releases:
            current_release = releases[0]
        if not current_release:
            raise HerokuAPIError("No releases found for this app.")

        payload: dict[str, Any] = {"description": "Redeploy triggered from Telegram bot"}
        slug = (current_release.get("slug") or {}).get("id")
        oci_image = (current_release.get("oci_image") or {}).get("id")

        if not slug and not oci_image:
            for artifact in current_release.get("artifacts", []):
                if artifact.get("type") == "slug" and artifact.get("id"):
                    slug = artifact["id"]
                if artifact.get("type") == "oci_image" and artifact.get("id"):
                    oci_image = artifact["id"]

        if slug:
            payload["slug"] = slug
        elif oci_image:
            payload["oci_image"] = oci_image
        else:
            raise HerokuAPIError("No slug or OCI image found to redeploy.")

        return await self._request(
            "POST",
            f"/apps/{app_name}/releases",
            json_data=payload,
        )

    async def restart_dynos(self, app_name: str) -> None:
        await self._request("DELETE", f"/apps/{app_name}/dynos")

    async def update_formation(
        self,
        app_name: str,
        updates: list[dict[str, int | str]],
    ) -> list[dict[str, Any]]:
        return await self._request(
            "PATCH",
            f"/apps/{app_name}/formation",
            json_data={"updates": updates},
        )

    async def stop_all_dynos(self, app_name: str) -> FormationBackup:
        formation = await self.get_formation(app_name)
        updates: list[dict[str, int | str]] = []
        backup: dict[str, int] = {}
        for dyno in formation:
            dyno_type = dyno["type"]
            quantity = int(dyno.get("quantity", 0))
            backup[dyno_type] = quantity
            updates.append({"type": dyno_type, "quantity": 0})

        if updates:
            await self.update_formation(app_name, updates)

        return FormationBackup(quantities=backup)

    async def start_all_dynos(
        self,
        app_name: str,
        backup_quantities: dict[str, int] | None = None,
    ) -> list[dict[str, Any]]:
        formation = await self.get_formation(app_name)
        updates: list[dict[str, int | str]] = []

        if backup_quantities:
            for dyno_type, quantity in backup_quantities.items():
                updates.append({"type": dyno_type, "quantity": max(1, int(quantity))})
        else:
            for dyno in formation:
                dyno_type = dyno["type"]
                current_quantity = int(dyno.get("quantity", 0))
                target_quantity = current_quantity if current_quantity > 0 else 1
                updates.append({"type": dyno_type, "quantity": target_quantity})

        if not updates and formation:
            first_type = formation[0]["type"]
            updates.append({"type": first_type, "quantity": 1})

        if not updates:
            raise HerokuAPIError("No dyno formation found for this app.")

        return await self.update_formation(app_name, updates)

    async def list_stacks(self) -> list[dict[str, Any]]:
        stacks = await self._request("GET", "/stacks", allow_404=True)
        return stacks or []

    async def change_stack(self, app_name: str, stack_name: str) -> dict[str, Any]:
        stacks = await self.list_stacks()
        stack_match = next((stack for stack in stacks if stack.get("name") == stack_name), None)
        payloads: list[dict[str, Any]] = []

        if stack_match and stack_match.get("id"):
            payloads.append({"build_stack": {"id": stack_match["id"]}})
            payloads.append({"stack": {"id": stack_match["id"]}})

        payloads.extend(
            [
                {"build_stack": {"name": stack_name}},
                {"stack": {"name": stack_name}},
                {"build_stack": stack_name},
                {"stack": stack_name},
            ]
        )

        last_error: HerokuAPIError | None = None
        for payload in payloads:
            try:
                return await self._request(
                    "PATCH",
                    f"/apps/{app_name}",
                    json_data=payload,
                )
            except HerokuAPIError as exc:
                last_error = exc

        raise last_error or HerokuAPIError("Unable to change stack.")
