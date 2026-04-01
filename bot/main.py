from __future__ import annotations

import asyncio
import html
import logging
import re
from contextlib import suppress
from io import BytesIO
from uuid import uuid4

import aiohttp

MAIN_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(MAIN_LOOP)

from pyrogram import Client, filters, idle
from pyrogram.enums import ChatMemberStatus, ChatType, ParseMode
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import BotCommand, CallbackQuery, Message

from bot.config import Settings, configure_logging
from bot.database import Database
from bot.heroku import HerokuAPIError, HerokuClient
from bot.keyboards import (
    add_api_keyboard,
    api_prompt_keyboard,
    app_input_keyboard,
    app_actions_keyboard,
    apps_keyboard,
    force_sub_keyboard,
    var_detail_keyboard,
    var_edit_keyboard,
    vars_keyboard,
    vps_bot_keyboard,
    vps_bot_prompt_keyboard,
    vps_bots_keyboard,
    vps_prompt_keyboard,
    vps_scan_menu_keyboard,
    vps_scan_results_keyboard,
    vps_server_keyboard,
    vps_servers_keyboard,
)
from bot.vps import DockerBotConfig, ScreenBotConfig, VPSAPIError, VPSClient, VPSServerConfig

LOGGER = logging.getLogger(__name__)
WAITING_API_STATE = "waiting_api_key"
WAITING_SET_VAR_PREFIX = "waiting_set_var"
WAITING_DEL_VAR_PREFIX = "waiting_del_var"
WAITING_EDIT_VAR_PREFIX = "waiting_edit_var"
WAITING_ADD_VPS_STATE = "waiting_add_vps"
WAITING_ADD_SCREEN_BOT_PREFIX = "waiting_add_screen_bot"
WAITING_SETUP_SCREEN_BOT_PREFIX = "waiting_setup_screen_bot"
SCREEN_SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
bot_username: str | None = None

settings = Settings.from_env()
configure_logging(settings.log_level)

app = Client(
    settings.session_name,
    api_id=settings.api_id,
    api_hash=settings.api_hash,
    bot_token=settings.bot_token,
    parse_mode=ParseMode.HTML,
)

db = Database(settings.mongo_uri, settings.mongo_db)
http_session: aiohttp.ClientSession | None = None
vps_client = VPSClient()


async def clear_bot_webhook() -> None:
    session = await get_http_session()
    url = f"https://api.telegram.org/bot{settings.bot_token}/deleteWebhook"
    async with session.post(url, json={"drop_pending_updates": True}) as response:
        payload = await response.json(content_type=None)
        if response.status >= 400 or not payload.get("ok"):
            LOGGER.warning("Failed to clear Telegram webhook: %s", payload)
        else:
            LOGGER.info("Telegram webhook cleared")


async def get_http_session() -> aiohttp.ClientSession:
    global http_session
    if http_session is None or http_session.closed:
        timeout = aiohttp.ClientTimeout(total=30)
        http_session = aiohttp.ClientSession(timeout=timeout)
    return http_session


async def get_heroku_client(user_id: int) -> HerokuClient:
    user = await db.get_user(user_id)
    if not user or not user.get("heroku_api_key"):
        raise HerokuAPIError("No Heroku API key saved. Use /myapps and add your API key first.")

    session = await get_http_session()
    return HerokuClient(user["heroku_api_key"], session)


def parse_chat_ref(channel: str) -> int | str:
    value = channel.strip()
    if value.lstrip("-").isdigit():
        return int(value)
    return value


async def get_force_sub_targets(client: Client) -> list[dict[str, int | str]]:
    targets: list[dict[str, int | str]] = []
    for index, channel in enumerate(settings.force_sub_channels):
        chat_ref = parse_chat_ref(channel)
        title = channel
        url = settings.force_sub_links[index] if index < len(settings.force_sub_links) else ""

        try:
            chat = await client.get_chat(chat_ref)
            title = getattr(chat, "title", None) or getattr(chat, "username", None) or channel

            if not url and getattr(chat, "username", None):
                url = f"https://t.me/{chat.username}"

            if not url and getattr(chat, "invite_link", None):
                url = chat.invite_link

            if not url:
                with suppress(Exception):
                    url = await client.export_chat_invite_link(chat.id)
        except Exception as exc:
            LOGGER.warning("Force-sub chat lookup failed for %s: %s", channel, exc)

        targets.append({"chat_ref": chat_ref, "label": str(title), "url": url})

    return targets


async def ensure_force_sub(client: Client, user_id: int) -> list[dict[str, int | str]]:
    missing: list[dict[str, int | str]] = []
    for target in await get_force_sub_targets(client):
        try:
            member = await client.get_chat_member(target["chat_ref"], user_id)
            if member.status in {
                ChatMemberStatus.LEFT,
                ChatMemberStatus.BANNED,
            }:
                missing.append(target)
        except Exception as exc:
            LOGGER.warning("Force-sub membership check failed for %s: %s", target["chat_ref"], exc)
            missing.append(target)
    return missing


async def require_subscription(message: Message | None, callback_query: CallbackQuery | None = None) -> bool:
    if not settings.force_sub_channels:
        return True

    user_id = message.from_user.id if message else callback_query.from_user.id
    missing = await ensure_force_sub(app, user_id)
    if not missing:
        return True

    channels_text = "\n".join(f"- {html.escape(str(target['label']))}" for target in missing)
    text = (
        "You need to join the required channels before using this bot.\n"
        f"{channels_text}\n\n"
        "Join all channels below, then tap refresh."
    )
    markup = force_sub_keyboard(missing)

    if callback_query:
        with suppress(MessageNotModified):
            await callback_query.message.edit_text(text, reply_markup=markup)
        await callback_query.answer("Join the required channels first.", show_alert=True)
    elif message:
        await message.reply_text(text, reply_markup=markup)
    return False


def is_private_message(message: Message) -> bool:
    return message.chat.type == ChatType.PRIVATE


async def reply_private_only(message: Message) -> None:
    target = f"https://t.me/{bot_username}" if bot_username else "my private chat"
    await message.reply_text(
        f"Use this bot in private for Heroku app management.\nOpen: {target}"
    )


def is_owner(user_id: int) -> bool:
    return user_id in settings.owner_ids


def api_key_prompt_text() -> str:
    return (
        "Send your Heroku API key in this chat.\n"
        "The bot will verify it\n\n"
        "If your key has many <code>_</code> characters, send it in one of these safe formats:\n"
        "<code>`HRKU-...`</code>\n"
        "<code>key: HRKU-...</code>"
    )


def normalize_heroku_api_key(raw_text: str) -> str:
    """
    Clean Heroku API key pasted by users in Telegram.

    Removes:
    - invisible unicode characters
    - backticks or quotes
    - spaces or newlines
    """

    if not raw_text:
        return ""

    key = raw_text.strip()

    # remove invisible characters sometimes added by Telegram
    key = key.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")

    # remove formatting characters
    key = key.replace("`", "").replace('"', "").replace("'", "")

    # remove spaces and newlines
    key = key.replace("\n", "").replace("\r", "").strip()

    return key


def state_for(prefix: str, app_name: str) -> str:
    return f"{prefix}:{app_name}"


def app_from_state(state: str | None, prefix: str) -> str | None:
    marker = f"{prefix}:"
    if state and state.startswith(marker):
        return state[len(marker) :]
    return None


def state_for_var(prefix: str, app_name: str, var_name: str) -> str:
    return f"{prefix}:{app_name}:{var_name}"


def var_from_state(state: str | None, prefix: str) -> tuple[str, str] | None:
    marker = f"{prefix}:"
    if not state or not state.startswith(marker):
        return None
    payload = state[len(marker) :]
    app_name, sep, var_name = payload.partition(":")
    if not sep or not app_name or not var_name:
        return None
    return app_name, var_name


def parse_mapping_message(raw_text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped or "=" not in stripped:
            continue
        key, value = stripped.split("=", maxsplit=1)
        data[key.strip().lower()] = value.strip()
    return data


def parse_config_var_updates(raw_text: str) -> dict[str, str]:
    updates: dict[str, str] = {}
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "=" not in stripped:
            raise ValueError("Each config var must be on its own line like KEY=VALUE.")
        key, value = stripped.split("=", maxsplit=1)
        key = key.strip()
        if not key:
            raise ValueError("Config var name cannot be empty.")
        updates[key] = value.strip()
    if not updates:
        raise ValueError("Send at least one config var like KEY=VALUE.")
    return updates


def mask_secret(value: str) -> str:
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"


def build_server_config(server: dict) -> VPSServerConfig:
    return VPSServerConfig(
        name=str(server.get("name", "")),
        host=str(server.get("host", "")),
        username=str(server.get("username", "")),
        password=str(server.get("password", "")),
        port=int(server.get("port", 22)),
    )


def build_screen_bot_config(bot_data: dict) -> ScreenBotConfig:
    return ScreenBotConfig(
        label=str(bot_data.get("label", "")),
        session_name=str(bot_data.get("session_name", "")),
        workdir=str(bot_data.get("workdir", "")),
        start_command=str(bot_data.get("start_command", "")),
    )


def build_docker_bot_config(bot_data: dict) -> DockerBotConfig:
    return DockerBotConfig(
        label=str(bot_data.get("label", "")),
        container_name=str(bot_data.get("container_name", "")),
    )


def bot_manager_type(bot_data: dict) -> str:
    manager_type = str(bot_data.get("manager_type", "screen")).strip().lower()
    if manager_type not in {"screen", "docker"}:
        return "screen"
    return manager_type


def state_for_vps_bot(prefix: str, server_id: str, bot_id: str) -> str:
    return f"{prefix}:{server_id}:{bot_id}"


def vps_bot_from_state(state: str | None, prefix: str) -> tuple[str, str] | None:
    marker = f"{prefix}:"
    if not state or not state.startswith(marker):
        return None
    payload = state[len(marker) :]
    server_id, sep, bot_id = payload.partition(":")
    if not sep or not server_id or not bot_id:
        return None
    return server_id, bot_id


def screen_bot_needs_setup(bot_data: dict) -> bool:
    return bot_manager_type(bot_data) == "screen" and (
        not str(bot_data.get("workdir", "")).strip() or not str(bot_data.get("start_command", "")).strip()
    )


async def find_existing_vps_bot(
    user_id: int,
    server_id: str,
    manager_type: str,
    identifier: str,
) -> dict | None:
    for bot_data in await db.list_vps_bots(user_id, server_id):
        if bot_manager_type(bot_data) != manager_type:
            continue
        if manager_type == "docker" and str(bot_data.get("container_name", "")) == identifier:
            return bot_data
        if manager_type == "screen" and str(bot_data.get("session_name", "")) == identifier:
            return bot_data
    return None


def format_formation(formation: list[dict]) -> str:
    if not formation:
        return "No dynos found."
    parts = []
    for item in formation:
        quantity = item.get("quantity", 0)
        size = item.get("size", "?")
        parts.append(f"{item['type']}: {quantity} dyno(s) [{size}]")
    return "\n".join(parts)


def format_var_value(value: str) -> str:
    if len(value) <= 3000:
        return value
    return f"{value[:3000]}\n\n...truncated..."


def format_log_preview(log_text: str, limit: int = 3500) -> str:
    if len(log_text) <= limit:
        return log_text
    return f"{log_text[-limit:]}\n\n...truncated to recent lines..."


def format_alert_text(*lines: str, limit: int = 180) -> str:
    text = "\n".join(line.strip() for line in lines if line and line.strip())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."


def current_stack(app_data: dict) -> str:
    build_stack = app_data.get("build_stack") or {}
    stack = app_data.get("stack") or {}
    return build_stack.get("name") or stack.get("name") or "Unknown"


async def render_apps(message: Message, user_id: int, page: int = 0) -> None:
    user = await db.get_user(user_id)
    if not user or not user.get("heroku_api_key"):
        await message.reply_text(
            "No Heroku API key saved yet.\nTap the button below and send your Heroku API key.",
            reply_markup=add_api_keyboard(),
        )
        return

    heroku = await get_heroku_client(user_id)
    apps = await heroku.list_apps()

    if not apps:
        await message.reply_text(
            "No Heroku apps were found for this API key.",
            reply_markup=add_api_keyboard(),
        )
        return

    account = user.get("account", {})
    account_line = account.get("email") or account.get("name") or "Connected account"
    text = (
        f"<b>Heroku Apps</b>\n"
        f"Account: <code>{html.escape(str(account_line))}</code>\n"
        f"Apps found: <b>{len(apps)}</b>\n\n"
        "Choose an app below."
    )
    await message.reply_text(text, reply_markup=apps_keyboard(apps, page))


async def render_add_api_prompt(callback_query: CallbackQuery, user_id: int) -> None:
    await db.set_state(user_id, WAITING_API_STATE)
    with suppress(MessageNotModified):
        await callback_query.message.edit_text(
            api_key_prompt_text(),
            reply_markup=api_prompt_keyboard(),
        )


async def render_apps_in_place(callback_query: CallbackQuery, user_id: int, page: int = 0) -> None:
    heroku = await get_heroku_client(user_id)
    apps = await heroku.list_apps()
    if not apps:
        with suppress(MessageNotModified):
            await callback_query.message.edit_text(
                "No Heroku apps were found for this API key.",
                reply_markup=add_api_keyboard(),
            )
        return

    text = f"<b>Heroku Apps</b>\nApps found: <b>{len(apps)}</b>\n\nChoose an app below."
    with suppress(MessageNotModified):
        await callback_query.message.edit_text(text, reply_markup=apps_keyboard(apps, page))


async def render_app_panel(callback_query: CallbackQuery, user_id: int, app_name: str) -> None:
    heroku = await get_heroku_client(user_id)
    app_data = await heroku.get_app(app_name)
    formation = await heroku.get_formation(app_name)
    config_vars = await heroku.get_config_vars(app_name)

    web_url = app_data.get("web_url") or "Not available"
    region = (app_data.get("region") or {}).get("name", "Unknown")
    stack = current_stack(app_data)
    var_names = sorted(config_vars.keys())
    config_preview = ", ".join(var_names[:6]) if var_names else "No config vars"
    if len(var_names) > 6:
        config_preview += ", ..."
    info = (
        f"<b>{html.escape(app_name)}</b>\n"
        f"Region: <code>{html.escape(region)}</code>\n"
        f"Stack: <code>{html.escape(stack)}</code>\n"
        f"URL: <code>{html.escape(web_url)}</code>\n\n"
        f"<b>Config Vars</b>: <code>{len(var_names)}</code>\n"
        f"<code>{html.escape(config_preview)}</code>\n\n"
        f"<b>Dyno Formation</b>\n{html.escape(format_formation(formation))}"
    )
    with suppress(MessageNotModified):
        await callback_query.message.edit_text(info, reply_markup=app_actions_keyboard(app_name))


async def render_vps_home(message: Message, user_id: int) -> None:
    servers = await db.list_vps_servers(user_id)
    if not servers:
        await message.reply_text(
            "No VPS saved yet.\n\n"
            "Tap below and send:\n"
            "<code>name=My VPS\nhost=1.2.3.4\nport=22\nusername=root\npassword=your-password</code>",
            reply_markup=vps_servers_keyboard([]),
        )
        return

    await message.reply_text(
        f"<b>Your VPS Servers</b>\nSaved servers: <b>{len(servers)}</b>\n\nChoose a server below.",
        reply_markup=vps_servers_keyboard(servers),
    )


async def render_vps_home_in_place(callback_query: CallbackQuery, user_id: int) -> None:
    servers = await db.list_vps_servers(user_id)
    if not servers:
        with suppress(MessageNotModified):
            await callback_query.message.edit_text(
                "No VPS saved yet.\n\n"
                "Tap below and send:\n"
                "<code>name=My VPS\nhost=1.2.3.4\nport=22\nusername=root\npassword=your-password</code>",
                reply_markup=vps_servers_keyboard([]),
            )
        return

    with suppress(MessageNotModified):
        await callback_query.message.edit_text(
            f"<b>Your VPS Servers</b>\nSaved servers: <b>{len(servers)}</b>\n\nChoose a server below.",
            reply_markup=vps_servers_keyboard(servers),
        )


async def get_vps_server_or_raise(user_id: int, server_id: str) -> dict:
    server = await db.get_vps_server(user_id, server_id)
    if not server:
        raise VPSAPIError("VPS server not found.")
    return server


async def get_vps_bot_or_raise(user_id: int, server_id: str, bot_id: str) -> dict:
    bot_data = await db.get_vps_bot(user_id, server_id, bot_id)
    if not bot_data:
        raise VPSAPIError("Screen bot not found.")
    return bot_data


async def render_vps_server_panel(callback_query: CallbackQuery, user_id: int, server_id: str) -> None:
    server = await get_vps_server_or_raise(user_id, server_id)
    bots = await db.list_vps_bots(user_id, server_id)
    text = (
        f"<b>{html.escape(str(server.get('name', 'VPS')))}</b>\n"
        f"Host: <code>{html.escape(str(server.get('host', '')))}</code>\n"
        f"Port: <code>{int(server.get('port', 22))}</code>\n"
        f"User: <code>{html.escape(str(server.get('username', '')))}</code>\n"
        f"Password: <code>{html.escape(mask_secret(str(server.get('password', ''))))}</code>\n\n"
        f"Saved bots: <b>{len(bots)}</b>"
    )
    with suppress(MessageNotModified):
        await callback_query.message.edit_text(text, reply_markup=vps_server_keyboard(server_id))


async def render_vps_scan_menu(callback_query: CallbackQuery, user_id: int, server_id: str) -> None:
    server = await get_vps_server_or_raise(user_id, server_id)
    with suppress(MessageNotModified):
        await callback_query.message.edit_text(
            f"<b>{html.escape(str(server.get('name', 'VPS')))}</b>\nChoose what to auto-detect and import.",
            reply_markup=vps_scan_menu_keyboard(server_id),
        )


async def render_vps_scan_results(
    callback_query: CallbackQuery,
    user_id: int,
    server_id: str,
    scan_type: str,
    *,
    page: int = 0,
) -> None:
    server = await get_vps_server_or_raise(user_id, server_id)
    items = await db.get_vps_scan_results(user_id, server_id, scan_type)
    label = "Docker containers" if scan_type == "docker" else "screen sessions"
    if not items:
        with suppress(MessageNotModified):
            await callback_query.message.edit_text(
                f"<b>{html.escape(str(server.get('name', 'VPS')))}</b>\nNo {label} found to import.",
                reply_markup=vps_scan_menu_keyboard(server_id),
            )
        return

    with suppress(MessageNotModified):
        await callback_query.message.edit_text(
            f"<b>{html.escape(str(server.get('name', 'VPS')))} {label}</b>\nTap one item to import it.",
            reply_markup=vps_scan_results_keyboard(server_id, scan_type, items, page),
        )


async def render_vps_bots_panel(callback_query: CallbackQuery, user_id: int, server_id: str) -> None:
    server = await get_vps_server_or_raise(user_id, server_id)
    bots = await db.list_vps_bots(user_id, server_id)
    if not bots:
        with suppress(MessageNotModified):
            await callback_query.message.edit_text(
                f"<b>{html.escape(str(server.get('name', 'VPS')))}</b>\nNo saved VPS bots yet.",
                reply_markup=vps_bots_keyboard(server_id, []),
            )
        return

    with suppress(MessageNotModified):
        await callback_query.message.edit_text(
            f"<b>{html.escape(str(server.get('name', 'VPS')))} bots</b>\n"
            f"Saved bots: <b>{len(bots)}</b>\n\nChoose one below.",
            reply_markup=vps_bots_keyboard(server_id, bots),
        )


async def render_vps_bot_panel(
    callback_query: CallbackQuery,
    user_id: int,
    server_id: str,
    bot_id: str,
) -> None:
    server = await get_vps_server_or_raise(user_id, server_id)
    bot_data = await get_vps_bot_or_raise(user_id, server_id, bot_id)
    manager_type = bot_manager_type(bot_data)
    needs_setup = screen_bot_needs_setup(bot_data)
    if manager_type == "docker":
        text = (
            f"<b>{html.escape(str(bot_data.get('label', 'Docker Bot')))}</b>\n"
            f"Server: <code>{html.escape(str(server.get('name', '')))}</code>\n"
            f"Manager: <code>docker</code>\n"
            f"Container: <code>{html.escape(str(bot_data.get('container_name', '')))}</code>"
        )
    else:
        text = (
            f"<b>{html.escape(str(bot_data.get('label', 'Screen Bot')))}</b>\n"
            f"Server: <code>{html.escape(str(server.get('name', '')))}</code>\n"
            f"Manager: <code>screen</code>\n"
            f"Session: <code>{html.escape(str(bot_data.get('session_name', '')))}</code>\n"
            f"Workdir: <code>{html.escape(str(bot_data.get('workdir', 'not set')))}</code>\n\n"
            f"Start command:\n<code>{html.escape(str(bot_data.get('start_command', 'not set')))}</code>"
        )
        if needs_setup:
            text += "\n\nImported session detected. Try Auto Setup first, or add workdir and command manually."
    with suppress(MessageNotModified):
        await callback_query.message.edit_text(
            text,
            reply_markup=vps_bot_keyboard(server_id, bot_id, manager_type, needs_setup=needs_setup),
        )


async def render_vars_panel(callback_query: CallbackQuery, user_id: int, app_name: str, page: int = 0) -> None:
    heroku = await get_heroku_client(user_id)
    config_vars = await heroku.get_config_vars(app_name)
    var_names = sorted(config_vars.keys())
    await db.save_var_keys(user_id, app_name, var_names)

    if not var_names:
        with suppress(MessageNotModified):
            await callback_query.message.edit_text(
                f"<b>{html.escape(app_name)}</b>\nNo config vars found.",
                reply_markup=app_input_keyboard(app_name),
            )
        return

    text = (
        f"<b>{html.escape(app_name)}</b>\n"
        f"Config vars: <b>{len(var_names)}</b>\n\n"
        "Choose a variable to view its full value."
    )
    with suppress(MessageNotModified):
        await callback_query.message.edit_text(text, reply_markup=vars_keyboard(app_name, var_names, page))


async def render_var_detail(
    callback_query: CallbackQuery,
    user_id: int,
    app_name: str,
    index: int,
) -> None:
    heroku = await get_heroku_client(user_id)
    config_vars = await heroku.get_config_vars(app_name)
    var_names = sorted(config_vars.keys())
    await db.save_var_keys(user_id, app_name, var_names)

    if index < 0 or index >= len(var_names):
        raise HerokuAPIError("Variable not found.")

    var_name = var_names[index]
    value = str(config_vars.get(var_name, ""))
    text = (
        f"<b>{html.escape(app_name)}</b>\n"
        f"<b>{html.escape(var_name)}</b>\n\n"
        f"<code>{html.escape(format_var_value(value))}</code>"
    )
    with suppress(MessageNotModified):
        await callback_query.message.edit_text(text, reply_markup=var_detail_keyboard(app_name, index))


async def monitor_redeploy(
    user_id: int,
    chat_id: int,
    app_name: str,
    release_id: str,
    release_version: int | None,
) -> None:
    try:
        heroku = await get_heroku_client(user_id)
        for _ in range(24):
            await asyncio.sleep(5)
            release = await heroku.get_release(app_name, release_id)
            status = str(release.get("status", "")).lower()
            version = release.get("version") or release_version or "?"

            if status == "succeeded":
                await app.send_message(
                    chat_id,
                    f"<b>{html.escape(app_name)}</b> redeploy completed successfully.\n"
                    f"Release: <code>v{version}</code>",
                )
                return

            if status in {"failed", "expired"}:
                failure_message = release.get("failure_message") or release.get("description") or "No failure details."
                await app.send_message(
                    chat_id,
                    f"<b>{html.escape(app_name)}</b> redeploy {html.escape(status)}.\n"
                    f"Release: <code>v{version}</code>\n"
                    f"Reason: <code>{html.escape(str(failure_message))}</code>",
                )
                return

        await app.send_message(
            chat_id,
            f"<b>{html.escape(app_name)}</b> redeploy is still pending.\n"
            "Check the app panel again in a bit for the latest status.",
        )
    except Exception as exc:
        LOGGER.warning("Redeploy monitor failed for %s: %s", app_name, exc)
        with suppress(Exception):
            await app.send_message(
                chat_id,
                f"<b>{html.escape(app_name)}</b> redeploy monitor stopped.\n"
                f"Reason: <code>{html.escape(str(exc))}</code>",
            )


async def set_bot_commands() -> None:
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("myapps", "Connect API and manage apps"),
        BotCommand("myvps", "Manage VPS bots"),
        BotCommand("help", "Show help"),
        BotCommand("ping", "Check bot status"),
        BotCommand("broadcast", "Admin only broadcast"),
    ]
    await app.set_bot_commands(commands)


async def broadcast_message(message: Message, body: str | None) -> None:
    if not is_owner(message.from_user.id):
        await message.reply_text("This command is only for bot owners.")
        return

    if message.reply_to_message:
        source_message = message.reply_to_message
        text_body = None
    else:
        source_message = None
        text_body = (body or "").strip()

    if not source_message and not text_body:
        await message.reply_text(
            "Use <code>/broadcast your message</code> or reply to a message with <code>/broadcast</code>."
        )
        return

    sent = 0
    failed = 0
    skipped = 0

    status = await message.reply_text("Broadcast started...")

    async for user_id in db.iter_user_ids():
        if user_id == message.from_user.id:
            skipped += 1
            continue
        try:
            if source_message:
                await source_message.copy(user_id)
            else:
                await app.send_message(user_id, text_body)
            sent += 1
        except FloodWait as exc:
            await asyncio.sleep(exc.value)
            try:
                if source_message:
                    await source_message.copy(user_id)
                else:
                    await app.send_message(user_id, text_body)
                sent += 1
            except Exception:
                failed += 1
        except Exception:
            failed += 1

    await status.edit_text(
        "Broadcast completed.\n"
        f"Sent: <b>{sent}</b>\n"
        f"Failed: <b>{failed}</b>\n"
        f"Skipped: <b>{skipped}</b>"
    )


@app.on_message(filters.command("start"))
async def start_handler(client: Client, message: Message) -> None:
    if not is_private_message(message):
        await reply_private_only(message)
        return
    await db.register_user(message.from_user)
    if not await require_subscription(message):
        return

    text = (
        "Welcome to Heroku Helper Bot.\n\n"
        "Use /myapps to manage Heroku apps and /myvps to manage VPS bots directly from Telegram."
    )
    await message.reply_text(text)


@app.on_message(filters.command("myapps"))
async def myapps_handler(client: Client, message: Message) -> None:
    if not is_private_message(message):
        await reply_private_only(message)
        return
    if not await require_subscription(message):
        return
    await render_apps(message, message.from_user.id)


@app.on_message(filters.command("myvps"))
async def myvps_handler(client: Client, message: Message) -> None:
    if not is_private_message(message):
        await reply_private_only(message)
        return
    if not await require_subscription(message):
        return
    await render_vps_home(message, message.from_user.id)


@app.on_message(filters.command("help"))
async def help_handler(client: Client, message: Message) -> None:
    if not is_private_message(message):
        await reply_private_only(message)
        return
    if not await require_subscription(message):
        return
    await message.reply_text(
        "Commands:\n"
        "/start - start the bot\n"
        "/myapps - connect API key and list Heroku apps\n"
        "/myvps - save VPS and manage screen or Docker bots\n"
        "/help - show this help"
    )


@app.on_message(filters.command("ping"))
async def ping_handler(client: Client, message: Message) -> None:
    await message.reply_text("Bot is online.")


@app.on_message(filters.command("broadcast"))
async def broadcast_handler(client: Client, message: Message) -> None:
    if not is_private_message(message):
        await reply_private_only(message)
        return
    body = message.text.split(maxsplit=1)[1] if message.text and " " in message.text else None
    await broadcast_message(message, body)


@app.on_message(group=-1)
async def incoming_update_logger(client: Client, message: Message) -> None:
    kind = "command" if message.text and message.text.startswith("/") else "message"
    LOGGER.info(
        "Incoming %s | chat_id=%s | chat_type=%s | user_id=%s",
        kind,
        getattr(message.chat, "id", None),
        getattr(message.chat, "type", None),
        getattr(message.from_user, "id", None),
    )


@app.on_message(filters.private & filters.text & ~filters.command(["start", "myapps", "myvps", "help", "ping", "broadcast"]))
async def api_capture_handler(client: Client, message: Message) -> None:
    if not await require_subscription(message):
        return

    state = await db.get_state(message.from_user.id)
    add_screen_bot_server = app_from_state(state, WAITING_ADD_SCREEN_BOT_PREFIX)
    setup_screen_bot = vps_bot_from_state(state, WAITING_SETUP_SCREEN_BOT_PREFIX)
    set_var_app = app_from_state(state, WAITING_SET_VAR_PREFIX)
    del_var_app = app_from_state(state, WAITING_DEL_VAR_PREFIX)
    edit_var_data = var_from_state(state, WAITING_EDIT_VAR_PREFIX)

    if state == WAITING_ADD_VPS_STATE:
        fields = parse_mapping_message(message.text)
        required_fields = ("name", "host", "username", "password")
        if any(not fields.get(field) for field in required_fields):
            await message.reply_text(
                "Send VPS details like this:\n"
                "<code>name=My VPS\nhost=1.2.3.4\nport=22\nusername=root\npassword=your-password</code>",
                reply_markup=vps_prompt_keyboard(),
            )
            return

        port_text = fields.get("port", "22")
        if not port_text.isdigit():
            await message.reply_text(
                "Port must be a number.\nExample: <code>port=22</code>",
                reply_markup=vps_prompt_keyboard(),
            )
            return

        server_id = uuid4().hex[:10]
        await db.save_vps_server(
            message.from_user.id,
            server_id,
            {
                "name": fields["name"],
                "host": fields["host"],
                "port": int(port_text),
                "username": fields["username"],
                "password": fields["password"],
            },
        )
        await db.set_state(message.from_user.id, None)
        await message.reply_text(
            f"VPS <b>{html.escape(fields['name'])}</b> saved.\nUse /myvps to test SSH and manage bots."
        )
        return

    if setup_screen_bot:
        server_id, bot_id = setup_screen_bot
        bot_data = await db.get_vps_bot(message.from_user.id, server_id, bot_id)
        if not bot_data or bot_manager_type(bot_data) != "screen":
            await db.set_state(message.from_user.id, None)
            await message.reply_text("That screen bot was not found anymore. Use /myvps and try again.")
            return

        fields = parse_mapping_message(message.text)
        if not fields.get("workdir") or not fields.get("command"):
            await message.reply_text(
                "Send the missing screen setup like this:\n"
                "<code>workdir=/root/mybot\ncommand=python3 bot.py</code>",
                reply_markup=vps_bot_prompt_keyboard(server_id),
            )
            return

        await db.save_vps_bot(
            message.from_user.id,
            server_id,
            bot_id,
            {
                **{key: value for key, value in bot_data.items() if key != "id"},
                "workdir": fields["workdir"],
                "start_command": fields["command"],
            },
        )
        await db.set_state(message.from_user.id, None)
        await message.reply_text(
            f"Setup completed for <b>{html.escape(str(bot_data.get('label', 'Screen Bot')))}</b>."
        )
        return

    if add_screen_bot_server:
        server = await db.get_vps_server(message.from_user.id, add_screen_bot_server)
        if not server:
            await db.set_state(message.from_user.id, None)
            await message.reply_text("That VPS was not found anymore. Use /myvps and try again.")
            return

        fields = parse_mapping_message(message.text)
        manager_type = fields.get("manager", "screen").strip().lower()
        if manager_type not in {"screen", "docker"}:
            await message.reply_text(
                "Manager must be <code>screen</code> or <code>docker</code>.",
                reply_markup=vps_bot_prompt_keyboard(add_screen_bot_server),
            )
            return

        bot_id = uuid4().hex[:10]
        if manager_type == "docker":
            required_fields = ("label", "container")
            if any(not fields.get(field) for field in required_fields):
                await message.reply_text(
                    "Send Docker bot details like this:\n"
                    "<code>manager=docker\nlabel=My Docker Bot\ncontainer=my-container</code>\n\n"
                    "For screen bots use:\n"
                    "<code>manager=screen\nlabel=My Bot\nsession=mybot\nworkdir=/root/mybot\ncommand=python3 bot.py</code>",
                    reply_markup=vps_bot_prompt_keyboard(add_screen_bot_server),
                )
                return

            payload = {
                "label": fields["label"],
                "manager_type": "docker",
                "container_name": fields["container"],
            }
        else:
            required_fields = ("label", "session", "workdir", "command")
            if any(not fields.get(field) for field in required_fields):
                await message.reply_text(
                    "Send screen bot details like this:\n"
                    "<code>manager=screen\nlabel=My Bot\nsession=mybot\nworkdir=/root/mybot\ncommand=python3 bot.py</code>\n\n"
                    "For Docker bots use:\n"
                    "<code>manager=docker\nlabel=My Docker Bot\ncontainer=my-container</code>",
                    reply_markup=vps_bot_prompt_keyboard(add_screen_bot_server),
                )
                return

            if not SCREEN_SESSION_RE.fullmatch(fields["session"]):
                await message.reply_text(
                    "Session name can only use letters, numbers, dot, underscore, and dash.",
                    reply_markup=vps_bot_prompt_keyboard(add_screen_bot_server),
                )
                return

            payload = {
                "label": fields["label"],
                "manager_type": "screen",
                "session_name": fields["session"],
                "workdir": fields["workdir"],
                "start_command": fields["command"],
            }

        await db.save_vps_bot(
            message.from_user.id,
            add_screen_bot_server,
            bot_id,
            payload,
        )
        await db.set_state(message.from_user.id, None)
        await message.reply_text(
            f"VPS bot <b>{html.escape(fields['label'])}</b> saved for "
            f"<b>{html.escape(str(server.get('name', 'VPS')))}</b>.\nUse /myvps to start or stop it."
        )
        return

    if set_var_app:
        try:
            updates = parse_config_var_updates(message.text)
        except ValueError as exc:
            await message.reply_text(
                "Send one or more config vars like this:\n"
                "<code>KEY=VALUE</code>\n"
                "<code>ANOTHER_KEY=ANOTHER_VALUE</code>\n\n"
                f"Error: <code>{html.escape(str(exc))}</code>",
                reply_markup=app_input_keyboard(set_var_app),
            )
            return

        heroku = await get_heroku_client(message.from_user.id)
        await heroku.update_config_vars(set_var_app, updates)
        await db.set_state(message.from_user.id, None)
        updated_keys = ", ".join(f"<code>{html.escape(key)}</code>" for key in sorted(updates.keys()))
        await message.reply_text(
            f"Updated <b>{len(updates)}</b> config var(s) for <b>{html.escape(set_var_app)}</b>.\n"
            f"{updated_keys}"
        )
        return

    if del_var_app:
        key = message.text.strip()
        if not key:
            await message.reply_text(
                "Send the config var name you want to remove.\nExample: <code>DATABASE_URL</code>",
                reply_markup=app_input_keyboard(del_var_app),
            )
            return

        heroku = await get_heroku_client(message.from_user.id)
        await heroku.update_config_vars(del_var_app, {key: None})
        await db.set_state(message.from_user.id, None)
        await message.reply_text(
            f"Config var <code>{html.escape(key)}</code> removed from <b>{html.escape(del_var_app)}</b>."
        )
        return

    if edit_var_data:
        app_name, var_name = edit_var_data
        value = message.text.strip()
        heroku = await get_heroku_client(message.from_user.id)
        await heroku.update_config_vars(app_name, {var_name: value})
        await db.set_state(message.from_user.id, None)
        await message.reply_text(
            f"Config var <code>{html.escape(var_name)}</code> updated for <b>{html.escape(app_name)}</b>."
        )
        return

    if state != WAITING_API_STATE:
        return

    api_key = normalize_heroku_api_key(message.text)
    heroku = HerokuClient(api_key, await get_http_session())

    try:
        account = await heroku.validate_token()
    except HerokuAPIError as exc:
        await message.reply_text(
            "That API key could not be verified.\n"
            f"Error: <code>{html.escape(str(exc))}</code>\n\n"
            "If Telegram changed repeated <code>_</code> characters, send it as "
            "<code>`HRKU-...`</code> or <code>key: HRKU-...</code>.\n\n"
            "Try again.",
            reply_markup=api_prompt_keyboard(),
        )
        return

    account_info = {
        "id": account.get("id"),
        "email": account.get("email"),
        "name": account.get("name"),
    }
    await db.save_api_key(message.from_user.id, api_key, account_info)

    await message.reply_text(
        "Heroku API key saved successfully.\nFetching your apps now..."
    )
    await render_apps(message, message.from_user.id)


@app.on_callback_query()
async def callback_router(client: Client, callback_query: CallbackQuery) -> None:
    if not await require_subscription(None, callback_query):
        return

    data = callback_query.data or ""
    user_id = callback_query.from_user.id

    try:
        if data == "forcesub:refresh":
            if await require_subscription(None, callback_query):
                with suppress(MessageNotModified):
                    await callback_query.message.edit_text(
                        "Subscription check passed.\nUse /myapps to continue."
                    )
                await callback_query.answer("You can use the bot now.")
            return

        if data == "api:add":
            await render_add_api_prompt(callback_query, user_id)
            await callback_query.answer("Waiting for your API key.")
            return

        if data == "api:cancel":
            await db.set_state(user_id, None)
            user = await db.get_user(user_id)
            if user and user.get("heroku_api_key"):
                await render_apps_in_place(callback_query, user_id)
            else:
                with suppress(MessageNotModified):
                    await callback_query.message.edit_text(
                        "No Heroku API key saved yet.\nTap the button below and send your Heroku API key.",
                        reply_markup=add_api_keyboard(),
                    )
            await callback_query.answer("Back.")
            return

        if data == "api:remove":
            await db.clear_api_key(user_id)
            with suppress(MessageNotModified):
                await callback_query.message.edit_text(
                    "Your saved API key has been removed.",
                    reply_markup=add_api_keyboard(),
                )
            await callback_query.answer("API key removed.")
            return

        if data == "vps:add":
            await db.set_state(user_id, WAITING_ADD_VPS_STATE)
            with suppress(MessageNotModified):
                await callback_query.message.edit_text(
                    "Send VPS details in this format:\n"
                    "<code>name=My VPS\nhost=1.2.3.4\nport=22\nusername=root\npassword=your-password</code>",
                    reply_markup=vps_prompt_keyboard(),
                )
            await callback_query.answer("Send VPS details.")
            return

        if data == "vps:back":
            await db.set_state(user_id, None)
            await render_vps_home_in_place(callback_query, user_id)
            await callback_query.answer()
            return

        if data.startswith("vpssrv:"):
            server_id = data.split(":", maxsplit=1)[1]
            await db.set_state(user_id, None)
            await render_vps_server_panel(callback_query, user_id, server_id)
            await callback_query.answer()
            return

        if data.startswith("vpsscanmenu:"):
            server_id = data.split(":", maxsplit=1)[1]
            await db.set_state(user_id, None)
            await render_vps_scan_menu(callback_query, user_id, server_id)
            await callback_query.answer()
            return

        if data.startswith("vpsscan:"):
            _, scan_type, server_id = data.split(":", maxsplit=2)
            server = await get_vps_server_or_raise(user_id, server_id)
            server_config = build_server_config(server)
            if scan_type == "docker":
                items = []
                for item in await vps_client.list_docker_containers(server_config, all_containers=True):
                    name = str(item.get("name", "")).strip()
                    image = str(item.get("image", "")).strip()
                    label = f"{name} ({image})" if image else name
                    items.append(
                        {
                            "label": label,
                            "value": name,
                            "image": image,
                            "status": str(item.get("status", "")),
                        }
                    )
            elif scan_type == "screen":
                items = []
                for item in await vps_client.list_screen_sessions(server_config):
                    _, _, session_name = item.partition(".")
                    clean_name = session_name or item
                    items.append({"label": clean_name, "value": clean_name})
            else:
                await callback_query.answer("Unknown scan type.", show_alert=True)
                return

            await db.save_vps_scan_results(user_id, server_id, scan_type, items)
            await render_vps_scan_results(callback_query, user_id, server_id, scan_type)
            await callback_query.answer("Scan completed.")
            return

        if data.startswith("vpsscanpage:"):
            _, scan_type, server_id, page_str = data.split(":", maxsplit=3)
            await render_vps_scan_results(
                callback_query,
                user_id,
                server_id,
                scan_type,
                page=int(page_str),
            )
            await callback_query.answer()
            return

        if data.startswith("vpsimport:"):
            _, scan_type, server_id, index_str = data.split(":", maxsplit=3)
            items = await db.get_vps_scan_results(user_id, server_id, scan_type)
            index = int(index_str)
            if index < 0 or index >= len(items):
                raise VPSAPIError("Scanned item not found. Run the scan again.")

            item = items[index]
            item_value = str(item.get("value", ""))
            item_label = str(item.get("label", item_value))
            if scan_type == "docker":
                existing = await find_existing_vps_bot(user_id, server_id, "docker", item_value)
                if existing:
                    await render_vps_bot_panel(callback_query, user_id, server_id, str(existing["id"]))
                    await callback_query.answer("Container already imported.")
                    return
                bot_id = uuid4().hex[:10]
                await db.save_vps_bot(
                    user_id,
                    server_id,
                    bot_id,
                    {
                        "label": item_label,
                        "manager_type": "docker",
                        "container_name": item_value,
                    },
                )
                await render_vps_bot_panel(callback_query, user_id, server_id, bot_id)
                await callback_query.answer("Docker bot imported.")
                return

            if scan_type == "screen":
                existing = await find_existing_vps_bot(user_id, server_id, "screen", item_value)
                if existing:
                    await render_vps_bot_panel(callback_query, user_id, server_id, str(existing["id"]))
                    await callback_query.answer("Screen session already imported.")
                    return
                bot_id = uuid4().hex[:10]
                await db.save_vps_bot(
                    user_id,
                    server_id,
                    bot_id,
                    {
                        "label": item_label,
                        "manager_type": "screen",
                        "session_name": item_value,
                        "workdir": "",
                        "start_command": "",
                    },
                )
                await render_vps_bot_panel(callback_query, user_id, server_id, bot_id)
                await callback_query.answer("Screen session imported.")
                return

            await callback_query.answer("Unknown import type.", show_alert=True)
            return

        if data.startswith("vpsact:"):
            _, action, server_id = data.split(":", maxsplit=2)
            server = await get_vps_server_or_raise(user_id, server_id)
            server_config = build_server_config(server)

            if action == "test":
                result = await vps_client.test_connection(server_config)
                await callback_query.message.reply_text(
                    f"<b>{html.escape(str(server.get('name', 'VPS')))} SSH test</b>\n\n"
                    f"<pre>{html.escape(result or 'Connected successfully.')}</pre>"
                )
                await callback_query.answer("SSH test completed.")
                return
            if action == "bots":
                await db.set_state(user_id, None)
                await render_vps_bots_panel(callback_query, user_id, server_id)
                await callback_query.answer()
                return
            if action == "scanmenu":
                await db.set_state(user_id, None)
                await render_vps_scan_menu(callback_query, user_id, server_id)
                await callback_query.answer()
                return
            if action == "addbot":
                await db.set_state(user_id, state_for(WAITING_ADD_SCREEN_BOT_PREFIX, server_id))
                with suppress(MessageNotModified):
                    await callback_query.message.edit_text(
                        "Send bot details in one of these formats:\n\n"
                        "<code>manager=screen\nlabel=My Bot\nsession=mybot\nworkdir=/root/mybot\ncommand=python3 bot.py</code>\n\n"
                        "<code>manager=docker\nlabel=My Docker Bot\ncontainer=my-container</code>",
                        reply_markup=vps_bot_prompt_keyboard(server_id),
                    )
                await callback_query.answer("Send bot details.")
                return
            if action == "sessions":
                sessions = await vps_client.list_screen_sessions(server_config)
                session_text = "\n".join(f"- <code>{html.escape(item)}</code>" for item in sessions)
                if not session_text:
                    session_text = "No running screen sessions found."
                await callback_query.message.reply_text(
                    f"<b>{html.escape(str(server.get('name', 'VPS')))} sessions</b>\n{session_text}"
                )
                await callback_query.answer("Sessions loaded.")
                return
            if action == "containers":
                containers = await vps_client.list_docker_containers(server_config, all_containers=True)
                container_lines: list[str] = []
                for item in containers:
                    name = str(item.get("name", "")).strip()
                    image = str(item.get("image", "")).strip()
                    status = str(item.get("status", "")).strip()
                    label = f"{name} ({image})" if image else name
                    if status:
                        container_lines.append(
                            f"- <code>{html.escape(label)}</code> : {html.escape(status)}"
                        )
                    else:
                        container_lines.append(f"- <code>{html.escape(label)}</code>")
                container_text = "\n".join(container_lines)
                if not container_text:
                    container_text = "No Docker containers found."
                await callback_query.message.reply_text(
                    f"<b>{html.escape(str(server.get('name', 'VPS')))} containers</b>\n{container_text}"
                )
                await callback_query.answer("Containers loaded.")
                return
            if action == "delete":
                await db.delete_vps_server(user_id, server_id)
                await db.set_state(user_id, None)
                await render_vps_home_in_place(callback_query, user_id)
                await callback_query.answer("VPS deleted.")
                return

            await callback_query.answer("Unknown VPS action.", show_alert=True)
            return

        if data.startswith("vpsbot:"):
            _, server_id, bot_id = data.split(":", maxsplit=2)
            await db.set_state(user_id, None)
            await render_vps_bot_panel(callback_query, user_id, server_id, bot_id)
            await callback_query.answer()
            return

        if data.startswith("vpsbotact:"):
            _, action, server_id, bot_id = data.split(":", maxsplit=3)
            server = await get_vps_server_or_raise(user_id, server_id)
            bot_data = await get_vps_bot_or_raise(user_id, server_id, bot_id)
            server_config = build_server_config(server)
            manager_type = bot_manager_type(bot_data)

            if action == "delete":
                await db.delete_vps_bot(user_id, server_id, bot_id)
                await render_vps_bots_panel(callback_query, user_id, server_id)
                await callback_query.answer("VPS bot deleted.")
                return
            if action == "autosetup":
                if manager_type != "screen":
                    await callback_query.answer("Auto setup only works for screen bots.", show_alert=True)
                    return
                guess = await vps_client.auto_detect_screen_setup(
                    server_config,
                    str(bot_data.get("session_name", "")),
                )
                await db.save_vps_bot(
                    user_id,
                    server_id,
                    bot_id,
                    {
                        **{key: value for key, value in bot_data.items() if key != "id"},
                        "workdir": guess.workdir,
                        "start_command": guess.command,
                    },
                )
                await render_vps_bot_panel(callback_query, user_id, server_id, bot_id)
                await callback_query.answer(
                    format_alert_text(
                        "Auto setup detected",
                        f"Workdir: {guess.workdir}",
                        f"Command: {guess.command}",
                    ),
                    show_alert=True,
                )
                return
            if action == "setup":
                await db.set_state(user_id, state_for_vps_bot(WAITING_SETUP_SCREEN_BOT_PREFIX, server_id, bot_id))
                with suppress(MessageNotModified):
                    await callback_query.message.edit_text(
                        "Send the missing screen setup like this:\n"
                        "<code>workdir=/root/mybot\ncommand=python3 bot.py</code>",
                        reply_markup=vps_bot_prompt_keyboard(server_id),
                    )
                await callback_query.answer("Send workdir and command.")
                return
            if manager_type == "docker":
                bot_config = build_docker_bot_config(bot_data)
                if action == "start":
                    await vps_client.start_docker_bot(server_config, bot_config)
                    await callback_query.answer("Docker bot started.")
                elif action == "stop":
                    await vps_client.stop_docker_bot(server_config, bot_config.container_name)
                    await callback_query.answer("Docker bot stopped.")
                elif action == "restart":
                    await vps_client.restart_docker_bot(server_config, bot_config)
                    await callback_query.answer("Docker bot restarted.")
                elif action == "status":
                    status_text = await vps_client.docker_container_status(server_config, bot_config.container_name)
                    await callback_query.answer(
                        format_alert_text(
                            f"{bot_config.label} container is {status_text}.",
                        ),
                        show_alert=True,
                    )
                    return
                elif action == "logs":
                    log_text = await vps_client.docker_logs(server_config, bot_config.container_name, tail=120)
                    preview = format_log_preview(log_text or "No Docker logs available.")
                    await callback_query.message.reply_text(
                        f"<b>{html.escape(bot_config.label)} Docker logs</b>\n\n"
                        f"<pre>{html.escape(preview)}</pre>"
                    )
                    await callback_query.answer("Docker logs sent.")
                    return
                else:
                    await callback_query.answer("Unknown Docker action.", show_alert=True)
                    return
            else:
                bot_config = build_screen_bot_config(bot_data)
                if action in {"start", "restart"} and screen_bot_needs_setup(bot_data):
                    raise VPSAPIError("This imported screen bot needs setup first. Tap Try Auto Setup or Manual Setup.")
                if action == "start":
                    await vps_client.start_screen_bot(server_config, bot_config)
                    await callback_query.answer("Screen bot started.")
                elif action == "stop":
                    await vps_client.stop_screen_bot(server_config, bot_config.session_name)
                    await callback_query.answer("Screen bot stopped.")
                elif action == "restart":
                    await vps_client.restart_screen_bot(server_config, bot_config)
                    await callback_query.answer("Screen bot restarted.")
                elif action == "status":
                    is_running = await vps_client.is_session_running(server_config, bot_config.session_name)
                    status_text = "running" if is_running else "stopped"
                    await callback_query.answer(
                        format_alert_text(
                            f"{bot_config.label} is currently {status_text}.",
                        ),
                        show_alert=True,
                    )
                    return
                elif action == "capture":
                    capture = await vps_client.capture_screen(server_config, bot_config.session_name)
                    preview = format_log_preview(capture or "No screen output available.")
                    await callback_query.message.reply_text(
                        f"<b>{html.escape(bot_config.label)} screen capture</b>\n\n"
                        f"<pre>{html.escape(preview)}</pre>"
                    )
                    await callback_query.answer("Screen capture sent.")
                    return
                else:
                    await callback_query.answer("Unknown screen action.", show_alert=True)
                    return

            await render_vps_bot_panel(callback_query, user_id, server_id, bot_id)
            return

        if data == "apps:back":
            await db.set_state(user_id, None)
            await render_apps_in_place(callback_query, user_id)
            await callback_query.answer()
            return

        if data.startswith("apps:page:"):
            page = int(data.rsplit(":", maxsplit=1)[1])
            await render_apps_in_place(callback_query, user_id, page=page)
            await callback_query.answer()
            return

        if data.startswith("app:"):
            app_name = data.split(":", maxsplit=1)[1]
            await db.set_state(user_id, None)
            await render_app_panel(callback_query, user_id, app_name)
            await callback_query.answer()
            return

        if data.startswith("vars:"):
            _, app_name, page_str = data.split(":", maxsplit=2)
            await db.set_state(user_id, None)
            await render_vars_panel(callback_query, user_id, app_name, page=int(page_str))
            await callback_query.answer()
            return

        if data.startswith("varshow:"):
            _, app_name, index_str = data.split(":", maxsplit=2)
            await db.set_state(user_id, None)
            await render_var_detail(callback_query, user_id, app_name, int(index_str))
            await callback_query.answer()
            return

        if data.startswith("varedit:"):
            _, app_name, index_str = data.split(":", maxsplit=2)
            index = int(index_str)
            var_names = await db.get_var_keys(user_id, app_name)
            if index < 0 or index >= len(var_names):
                raise HerokuAPIError("Variable not found.")
            var_name = var_names[index]
            await db.set_state(user_id, state_for_var(WAITING_EDIT_VAR_PREFIX, app_name, var_name))
            with suppress(MessageNotModified):
                await callback_query.message.edit_text(
                    f"Send the new value for <code>{html.escape(var_name)}</code> in <b>{html.escape(app_name)}</b>.",
                    reply_markup=var_edit_keyboard(app_name, index),
                )
            await callback_query.answer("Send the new value.")
            return

        if data.startswith("vardel:"):
            _, app_name, index_str = data.split(":", maxsplit=2)
            index = int(index_str)
            var_names = await db.get_var_keys(user_id, app_name)
            if index < 0 or index >= len(var_names):
                raise HerokuAPIError("Variable not found.")
            var_name = var_names[index]
            heroku = await get_heroku_client(user_id)
            await heroku.update_config_vars(app_name, {var_name: None})
            await db.set_state(user_id, None)
            await callback_query.answer("Variable removed.")
            await render_vars_panel(callback_query, user_id, app_name)
            return

        if data.startswith("action:"):
            _, action, app_name = data.split(":", maxsplit=2)
            heroku = await get_heroku_client(user_id)

            if action == "start":
                backup = await db.get_formation_backup(user_id, app_name)
                await heroku.start_all_dynos(app_name, backup)
                await callback_query.answer("Dynos started.")
            elif action == "stop":
                backup = await heroku.stop_all_dynos(app_name)
                await db.save_formation_backup(user_id, app_name, backup.quantities)
                await callback_query.answer("Dynos stopped.")
            elif action == "restart":
                await heroku.restart_dynos(app_name)
                await callback_query.answer("Dynos restarted.")
            elif action == "redeploy":
                release = await heroku.redeploy_app(app_name)
                release_id = release.get("id")
                release_version = release.get("version")
                if release_id:
                    asyncio.create_task(
                        monitor_redeploy(
                            user_id,
                            callback_query.message.chat.id,
                            app_name,
                            release_id,
                            release_version,
                        )
                    )
                await callback_query.answer("Redeploy requested.")
                await callback_query.message.reply_text(
                    f"Redeploy started for <b>{html.escape(app_name)}</b>.\n"
                    "I will send you another message when it completes."
                )
            elif action == "logs":
                log_text = await heroku.get_logs(app_name, lines=120, source="app", tail=False)
                preview = format_log_preview(log_text or "No logs available.")
                await callback_query.message.reply_text(
                    f"<b>{html.escape(app_name)} recent logs</b>\n\n"
                    f"<pre>{html.escape(preview)}</pre>"
                )
                await callback_query.answer("Recent logs sent.")
                return
            elif action == "logfile":
                log_text = await heroku.get_logs(app_name, lines=400, source="app", tail=False)
                payload = BytesIO((log_text or "No logs available.\n").encode("utf-8"))
                payload.name = f"{app_name}-log.txt"
                await callback_query.message.reply_document(
                    payload,
                    caption=f"{app_name} logs",
                    file_name=f"{app_name}-log.txt",
                )
                await callback_query.answer("Log file sent.")
                return
            elif action == "setvar":
                await db.set_state(user_id, state_for(WAITING_SET_VAR_PREFIX, app_name))
                with suppress(MessageNotModified):
                    await callback_query.message.edit_text(
                        f"Send one or more config vars for <b>{html.escape(app_name)}</b> like this:\n"
                        "<code>KEY=VALUE</code>\n"
                        "<code>ANOTHER_KEY=ANOTHER_VALUE</code>",
                        reply_markup=app_input_keyboard(app_name),
                    )
                await callback_query.answer("Send one or more KEY=VALUE lines.")
                return
            elif action == "delvar":
                await db.set_state(user_id, state_for(WAITING_DEL_VAR_PREFIX, app_name))
                with suppress(MessageNotModified):
                    await callback_query.message.edit_text(
                        f"Send the config var name you want to remove from <b>{html.escape(app_name)}</b>.\n"
                        "Example: <code>DATABASE_URL</code>",
                        reply_markup=app_input_keyboard(app_name),
                    )
                await callback_query.answer("Send the var name.")
                return
            elif action == "viewvars":
                await db.set_state(user_id, None)
                await render_vars_panel(callback_query, user_id, app_name)
                await callback_query.answer()
                return
            elif action == "stack24":
                await heroku.change_stack(app_name, "heroku-24")
                await callback_query.answer("Stack change requested.")
            elif action == "docker":
                await heroku.change_stack(app_name, "container")
                await callback_query.answer("Docker stack requested.")
            elif action == "refresh":
                await callback_query.answer("Refreshing app info.")
            else:
                await callback_query.answer("Unknown action.", show_alert=True)
                return

            await render_app_panel(callback_query, user_id, app_name)
            return

        await callback_query.answer("Unknown button.", show_alert=True)
    except HerokuAPIError as exc:
        LOGGER.warning("Heroku API error while handling callback: %s", exc)
        await callback_query.answer(str(exc), show_alert=True)
    except VPSAPIError as exc:
        LOGGER.warning("VPS error while handling callback: %s", exc)
        await callback_query.answer(str(exc), show_alert=True)
    except Exception as exc:
        LOGGER.exception("Unexpected callback error")
        await callback_query.answer(f"Unexpected error: {exc}", show_alert=True)


async def startup() -> None:
    await db.setup()
    await clear_bot_webhook()
    LOGGER.info("Database indexes ready")


async def after_startup() -> None:
    global bot_username
    me = await app.get_me()
    bot_username = me.username
    await set_bot_commands()
    LOGGER.info(
        "Authorized bot: @%s (%s) | force_sub_channels=%s | owner_ids=%s",
        me.username,
        me.id,
        settings.force_sub_channels,
        settings.owner_ids,
    )


async def shutdown() -> None:
    if http_session and not http_session.closed:
        await http_session.close()


async def run() -> None:
    await startup()
    try:
        async with app:
            await after_startup()
            LOGGER.info("Bot started")
            await idle()
    finally:
        await shutdown()


def main() -> None:
    try:
        MAIN_LOOP.run_until_complete(run())
    finally:
        MAIN_LOOP.close()


if __name__ == "__main__":
    main()
