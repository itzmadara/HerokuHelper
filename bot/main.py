from __future__ import annotations

import asyncio
import html
import logging
import re
from contextlib import suppress

import aiohttp

MAIN_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(MAIN_LOOP)

from pyrogram import Client, filters, idle
from pyrogram.enums import ChatMemberStatus, ChatType, ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import CallbackQuery, Message

from bot.config import Settings, configure_logging
from bot.database import Database
from bot.heroku import HerokuAPIError, HerokuClient
from bot.keyboards import (
    add_api_keyboard,
    api_prompt_keyboard,
    app_actions_keyboard,
    apps_keyboard,
    force_sub_keyboard,
)

LOGGER = logging.getLogger(__name__)
WAITING_API_STATE = "waiting_api_key"
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


def normalize_heroku_api_key(raw_text: str) -> str:
    return raw_text.strip().replace("`", "").replace('"', "")


def format_formation(formation: list[dict]) -> str:
    if not formation:
        return "No dynos found."
    parts = []
    for item in formation:
        quantity = item.get("quantity", 0)
        size = item.get("size", "?")
        parts.append(f"{item['type']}: {quantity} dyno(s) [{size}]")
    return "\n".join(parts)


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
            "Send your Heroku API key in this chat.\n"
            "The bot will verify it and save it in MongoDB.",
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

    web_url = app_data.get("web_url") or "Not available"
    region = (app_data.get("region") or {}).get("name", "Unknown")
    stack = current_stack(app_data)
    info = (
        f"<b>{html.escape(app_name)}</b>\n"
        f"Region: <code>{html.escape(region)}</code>\n"
        f"Stack: <code>{html.escape(stack)}</code>\n"
        f"URL: <code>{html.escape(web_url)}</code>\n\n"
        f"<b>Dyno Formation</b>\n{html.escape(format_formation(formation))}"
    )
    with suppress(MessageNotModified):
        await callback_query.message.edit_text(info, reply_markup=app_actions_keyboard(app_name))


@app.on_message(filters.command("start"))
async def start_handler(client: Client, message: Message) -> None:
    if not is_private_message(message):
        await reply_private_only(message)
        return
    if not await require_subscription(message):
        return

    text = (
        "Welcome to Heroku Helper Bot.\n\n"
        "Use /myapps to connect your Heroku API key and manage your apps from inline buttons."
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
        "/help - show this help"
    )


@app.on_message(filters.command("ping"))
async def ping_handler(client: Client, message: Message) -> None:
    await message.reply_text("Bot is online.")


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


@app.on_message(filters.private & filters.text & ~filters.command(["start", "myapps", "help"]))
async def api_capture_handler(client: Client, message: Message) -> None:
    if not await require_subscription(message):
        return

    state = await db.get_state(message.from_user.id)
    if state != WAITING_API_STATE:
        return

    api_key = normalize_heroku_api_key(message.text)
    heroku = HerokuClient(api_key, await get_http_session())

    try:
        account = await heroku.validate_token()
    except HerokuAPIError as exc:
        await message.reply_text(
            f"That API key could not be verified.\nError: <code>{html.escape(str(exc))}</code>\n\nTry again.",
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

        if data == "apps:back":
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
            await render_app_panel(callback_query, user_id, app_name)
            await callback_query.answer()
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
        LOGGER.exception("Heroku API error while handling callback")
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
    LOGGER.info(
        "Authorized bot: @%s (%s) | force_sub_channels=%s",
        me.username,
        me.id,
        settings.force_sub_channels,
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
