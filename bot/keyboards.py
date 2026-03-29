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
                InlineKeyboardButton("View Logs", callback_data=f"action:logs:{app_name}"),
            ],
            [
                InlineKeyboardButton("Log File", callback_data=f"action:logfile:{app_name}"),
                InlineKeyboardButton("Dyno On", callback_data=f"action:start:{app_name}"),
            ],
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


def vps_servers_keyboard(servers: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    buttons = [
        InlineKeyboardButton(server["name"], callback_data=f"vpssrv:{server['id']}")
        for server in servers
    ]
    rows.extend(_chunk(buttons, 2))
    rows.append([InlineKeyboardButton("Add VPS", callback_data="vps:add")])
    return InlineKeyboardMarkup(rows)


def vps_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Back", callback_data="vps:back")]]
    )


def vps_server_keyboard(server_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Test SSH", callback_data=f"vpsact:test:{server_id}"),
                InlineKeyboardButton("View Bots", callback_data=f"vpsact:bots:{server_id}"),
            ],
            [
                InlineKeyboardButton("Add Bot", callback_data=f"vpsact:addbot:{server_id}"),
                InlineKeyboardButton("Sessions", callback_data=f"vpsact:sessions:{server_id}"),
            ],
            [
                InlineKeyboardButton("Containers", callback_data=f"vpsact:containers:{server_id}"),
                InlineKeyboardButton("Scan Import", callback_data=f"vpsact:scanmenu:{server_id}"),
            ],
            [
                InlineKeyboardButton("Delete VPS", callback_data=f"vpsact:delete:{server_id}"),
            ],
            [
                InlineKeyboardButton("Back", callback_data="vps:back"),
            ],
        ]
    )


def vps_bots_keyboard(server_id: str, bots: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    buttons = [
        InlineKeyboardButton(bot["label"], callback_data=f"vpsbot:{server_id}:{bot['id']}")
        for bot in bots
    ]
    rows.extend(_chunk(buttons, 2))
    rows.append([InlineKeyboardButton("Add Bot", callback_data=f"vpsact:addbot:{server_id}")])
    rows.append([InlineKeyboardButton("Back", callback_data=f"vpssrv:{server_id}")])
    return InlineKeyboardMarkup(rows)


def vps_bot_prompt_keyboard(server_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Back", callback_data=f"vpssrv:{server_id}")]]
    )


def vps_bot_keyboard(
    server_id: str,
    bot_id: str,
    manager_type: str,
    *,
    needs_setup: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("Start", callback_data=f"vpsbotact:start:{server_id}:{bot_id}"),
            InlineKeyboardButton("Stop", callback_data=f"vpsbotact:stop:{server_id}:{bot_id}"),
        ],
        [
            InlineKeyboardButton("Restart", callback_data=f"vpsbotact:restart:{server_id}:{bot_id}"),
            InlineKeyboardButton("Status", callback_data=f"vpsbotact:status:{server_id}:{bot_id}"),
        ],
    ]

    if manager_type == "docker":
        rows.append(
            [
                InlineKeyboardButton("Logs", callback_data=f"vpsbotact:logs:{server_id}:{bot_id}"),
                InlineKeyboardButton("Delete", callback_data=f"vpsbotact:delete:{server_id}:{bot_id}"),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton("Capture", callback_data=f"vpsbotact:capture:{server_id}:{bot_id}"),
                InlineKeyboardButton("Delete", callback_data=f"vpsbotact:delete:{server_id}:{bot_id}"),
            ]
        )

    if needs_setup:
        rows.append(
            [
                InlineKeyboardButton("Try Auto Setup", callback_data=f"vpsbotact:autosetup:{server_id}:{bot_id}"),
                InlineKeyboardButton("Manual Setup", callback_data=f"vpsbotact:setup:{server_id}:{bot_id}"),
            ]
        )

    rows.append([InlineKeyboardButton("Back", callback_data=f"vpsact:bots:{server_id}")])
    return InlineKeyboardMarkup(rows)


def vps_scan_menu_keyboard(server_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Scan Screen", callback_data=f"vpsscan:screen:{server_id}"),
                InlineKeyboardButton("Scan Docker", callback_data=f"vpsscan:docker:{server_id}"),
            ],
            [
                InlineKeyboardButton("Back", callback_data=f"vpssrv:{server_id}"),
            ],
        ]
    )

def vps_scan_results_keyboard(
    server_id: str,
    scan_type: str,
    items: list[dict[str, str]],
    page: int,
    page_size: int = 8,
) -> InlineKeyboardMarkup:
    start = page * page_size
    end = start + page_size
    page_items = items[start:end]

    rows: list[list[InlineKeyboardButton]] = []
    buttons = [
        InlineKeyboardButton(
            (item["label"][:57] + "...") if len(item["label"]) > 60 else item["label"],
            callback_data=f"vpsimport:{scan_type}:{server_id}:{start + index}",
        )
        for index, item in enumerate(page_items)
    ]
    rows.extend(_chunk(buttons, 1))

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(
            InlineKeyboardButton("Previous", callback_data=f"vpsscanpage:{scan_type}:{server_id}:{page - 1}")
        )
    if end < len(items):
        nav_row.append(
            InlineKeyboardButton("Next", callback_data=f"vpsscanpage:{scan_type}:{server_id}:{page + 1}")
        )
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("Back", callback_data=f"vpsscanmenu:{server_id}")])
    return InlineKeyboardMarkup(rows)
