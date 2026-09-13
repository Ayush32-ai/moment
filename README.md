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

## Authentication for this MVP

Pass an arbitrary stable user identifier via `X-User-Id`. This is deliberately a development placeholder; replace `current_user` with JWT/OAuth verification before deployment.

## Processing lifecycle

`draft` → `collecting` → `processing` → `ready` (or `failed`). Upload completion moves a moment to `collecting`; calling the processing endpoint starts a simulated reconstruction job. A real worker should consume that job and call the status endpoint after feature matching, alignment, and reconstruction complete.

