# Heroku Helper Bot

Pyrogram Telegram bot that lets users connect their Heroku API key, save it in MongoDB, list their apps, and manage basic actions from inline buttons.
It also supports saving VPS servers and controlling `screen`-managed or Docker-based bot processes over SSH.

## Features

- `/start` welcome flow with force-subscription support for one or more channels.
- `/start` stores the user in MongoDB for future broadcasts.
- `/myapps` prompts users to add a Heroku API key if they have not connected one yet.
- `/myvps` lets users save VPS credentials and manage `screen` or Docker bots.
- VPS scan/import flow can auto-detect running `screen` sessions and Docker containers for one-tap import.
- VPS container view can clean up stopped Docker containers and remove matching saved Docker bot entries.
- Automatic Telegram bot command setup on startup.
- Owner-only `/broadcast` command for sending a message to all saved users.
- Lists Heroku apps in inline buttons with up to 6 buttons per row.
- App panel for:
  - dyno start
  - dyno stop
  - dyno restart
  - stack switch to `heroku-24`
  - stack switch to `container` (Docker)
- MongoDB storage for user API keys, state, and previous dyno quantities.
- MongoDB storage for VPS server credentials and saved VPS bot definitions.

## Setup

1. Create a Telegram bot with BotFather.
2. Create a Telegram API app to get `API_ID` and `API_HASH`.
3. Create a MongoDB database.
4. Copy `.env.example` to `.env` and fill in your values.
5. Install dependencies:

```bash
pip install -r requirements.txt
```

6. Use Python 3.13 for local/dev or let Heroku read `runtime.txt`.

7. Run the bot:

```bash
python -m bot.main
```

## Force Subscription

- Set `FORCE_SUB_CHANNELS` to a comma-separated list of usernames or channel IDs.
- Set `FORCE_SUB_LINKS` to matching invite links if you want join buttons.
- The bot checks membership before allowing `/start`, `/myapps`, and callback actions.

## Broadcast

- Set `OWNER_IDS` to one or more Telegram user IDs separated by commas.
- Use `/broadcast your text here` to send a text broadcast.
- You can also reply to any message with `/broadcast` to forward that message to all saved users.

## Notes

- Heroku API keys are stored in MongoDB because the bot needs them to manage user apps.
- VPS passwords are also stored in MongoDB so the bot can open SSH sessions on behalf of the user.
- For production, use a private MongoDB deployment and restrict database access.
- Changing stacks on Heroku can trigger rebuild or release-related behavior depending on the app.
- VPS `screen` support expects the remote server to have `screen` installed and available in `PATH`.
- Docker support expects the remote server user to be allowed to run `docker` commands.
