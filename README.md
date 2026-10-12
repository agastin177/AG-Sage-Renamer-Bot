# Telegram File Renamer Bot

Rename Telegram files up to 2GB (bot) or 4GB (with Premium helper account).

## Deploy
1. Push this repo to GitHub.
2. Connect the repo on Railway / Koyeb / Render (Dockerfile is detected automatically).
3. Add the environment variables from `.env.example` in the dashboard.

## Run locally
```
pip install -r requirements.txt
python bot.py
```

Never commit your `.env`, BOT_TOKEN or SESSION_STRING.
