from __future__ import annotations

from itertools import zip_longest

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def _chunk(items: list[InlineKeyboardButton], size: int) -> list[list[InlineKeyboardButton]]:
    if not items:
        return []
    args = [iter(items)] * size
    rows: list[list[InlineKeyboardButton]] = []
    for group in zip_longest(*args):
        row = [button for button in group if button is not None]
        rows.append(row)
    return rows


def force_sub_keyboard(targets: list[dict[str, str]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for target in targets:
        label = target["label"]
        url = target.get("url")
        if url:
            rows.append([InlineKeyboardButton(f"Join {label}", url=url)])
    rows.append([InlineKeyboardButton("I Joined, Refresh", callback_data="forcesub:refresh")])
    return InlineKeyboardMarkup(rows)


def apps_keyboard(apps: list[dict], page: int, page_size: int = 24) -> InlineKeyboardMarkup:
    start = page * page_size
    end = start + page_size
    page_apps = apps[start:end]

    buttons = [
        InlineKeyboardButton(app["name"], callback_data=f"app:{app['name']}")
        for app in page_apps
    ]
    rows = _chunk(buttons, 3)

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("Previous", callback_data=f"apps:page:{page - 1}"))
    if end < len(apps):
        nav_row.append(InlineKeyboardButton("Next", callback_data=f"apps:page:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("Replace API Key", callback_data="api:add")])
    rows.append([InlineKeyboardButton("Remove API Key", callback_data="api:remove")])
    return InlineKeyboardMarkup(rows)


def add_api_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Let's Add Your API", callback_data="api:add")]]
    )


def api_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Back", callback_data="api:cancel")]]
    )


def app_input_keyboard(app_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Back", callback_data=f"app:{app_name}")]]
    )


def vars_keyboard(app_name: str, var_names: list[str], page: int, page_size: int = 12) -> InlineKeyboardMarkup:
    start = page * page_size
    end = start + page_size
    page_vars = var_names[start:end]

    buttons = [
        InlineKeyboardButton(var_name, callback_data=f"varshow:{app_name}:{start + index}")
        for index, var_name in enumerate(page_vars)
    ]
    rows = _chunk(buttons, 2)

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("Previous", callback_data=f"vars:{app_name}:{page - 1}"))
    if end < len(var_names):
        nav_row.append(InlineKeyboardButton("Next", callback_data=f"vars:{app_name}:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("Back", callback_data=f"app:{app_name}")])
    return InlineKeyboardMarkup(rows)


def var_detail_keyboard(app_name: str, index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Edit Value", callback_data=f"varedit:{app_name}:{index}"),
                InlineKeyboardButton("Delete", callback_data=f"vardel:{app_name}:{index}"),
            ],
            [
                InlineKeyboardButton("Back to Vars", callback_data=f"vars:{app_name}:0"),
                InlineKeyboardButton("Back to App", callback_data=f"app:{app_name}"),
            ],
        ]
    )


def var_edit_keyboard(app_name: str, index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Back", callback_data=f"varshow:{app_name}:{index}")]]
    )


def app_actions_keyboard(app_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Redeploy", callback_data=f"action:redeploy:{app_name}"),
                InlineKeyboardButton("Refresh", callback_data=f"action:refresh:{app_name}"),
            ],
            [
                InlineKeyboardButton("View Vars", callback_data=f"action:viewvars:{app_name}"),
                InlineKeyboardButton("Set Var", callback_data=f"action:setvar:{app_name}"),
            ],
            [
                InlineKeyboardButton("Remove Var", callback_data=f"action:delvar:{app_name}"),
            ],
            [InlineKeyboardButton("Dyno On", callback_data=f"action:start:{app_name}")],
            [InlineKeyboardButton("Dyno Off", callback_data=f"action:stop:{app_name}")],
            [InlineKeyboardButton("Restart Dynos", callback_data=f"action:restart:{app_name}")],
            [
                InlineKeyboardButton("Set heroku-24", callback_data=f"action:stack24:{app_name}"),
                InlineKeyboardButton("Set Docker", callback_data=f"action:docker:{app_name}"),
            ],
            [
                InlineKeyboardButton("Back", callback_data="apps:back"),
            ],
        ]
    )
