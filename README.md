# AI Venture Studio — MVP v0.1

Foundation for a 24/7 AI Venture Studio.

## Current scope

- FastAPI control API
- PostgreSQL system of record
- Opportunity scoring engine
- Telegram high-confidence opportunity alerts
- Docker Compose deployment
- Obsidian Company Brain scaffold
- Human-approval and audit entities prepared in the schema

## Start locally/on a Linux VPS

```bash
cp .env.example .env
# Change POSTGRES_PASSWORD. Add Telegram values later.
docker compose up -d --build
curl http://localhost:8000/api/health
```

API docs: `http://localhost:8000/docs`

## Security

Never commit `.env`, API keys, Telegram bot tokens, SSH keys, or production secrets.

## MVP flow

`collect -> normalize -> opportunity -> research -> critic -> score -> Telegram review -> experiment proposal`

The first implementation intentionally does not contain autonomous spending, production deployment, DNS changes, or destructive actions.
