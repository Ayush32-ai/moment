# Moment API

An MVP backend for building shared, multi-perspective experiences. It persists data in SQLite for local development and exposes a REST API documented at `/docs`.

## Run locally

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Set `MOMENT_DATABASE_PATH` to use a different SQLite database file. The default is `moment.db` in the project directory.

## Deploy on Render

Create a Render Web Service connected to this repository and use Docker as the environment. Render will detect the root `Dockerfile` automatically. Set the health check path to `/health`.

The current deployment uses SQLite, which is suitable for a demo but not durable on Render's ephemeral filesystem. Use a Render persistent disk or migrate the database layer to Postgres before production use.

## Authentication for this MVP

Pass an arbitrary stable user identifier via `X-User-Id`. This is deliberately a development placeholder; replace `current_user` with JWT/OAuth verification before deployment.

## Processing lifecycle

`draft` → `collecting` → `processing` → `ready` (or `failed`). Upload completion moves a moment to `collecting`; calling the processing endpoint starts a simulated reconstruction job. A real worker should consume that job and call the status endpoint after feature matching, alignment, and reconstruction complete.

