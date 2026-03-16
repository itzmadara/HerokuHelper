# Heroku Helper Bot

Pyrogram Telegram bot that lets users connect their Heroku API key, save it in MongoDB, list their apps, and manage basic actions from inline buttons.

## Features

- `/start` welcome flow with force-subscription support for one or more channels.
- `/myapps` prompts users to add a Heroku API key if they have not connected one yet.
- Lists Heroku apps in inline buttons with up to 6 buttons per row.
- App panel for:
  - dyno start
  - dyno stop
  - dyno restart
  - stack switch to `heroku-24`
  - stack switch to `container` (Docker)
- MongoDB storage for user API keys, state, and previous dyno quantities.

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

## Notes

- Heroku API keys are stored in MongoDB because the bot needs them to manage user apps.
- For production, use a private MongoDB deployment and restrict database access.
- Changing stacks on Heroku can trigger rebuild or release-related behavior depending on the app.
