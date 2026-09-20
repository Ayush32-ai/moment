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

## Add media to a Moment

Create a Moment first, then send a `multipart/form-data` request to
`POST /moments/{moment_id}/contributions` (or the canonical `/v1` path).
The request must include `X-User-Id`, `captured_at`, and
`consent_to_reconstruct=true`. Provide exactly one of:

- `file`: an image, video, or audio file. Browser camera and microphone recordings can be sent as `Blob` or `File` objects.
- `spotify_url`: an HTTPS Spotify track URL. The API stores the link and does not download or copy Spotify audio.

Example file upload:

```powershell
curl.exe -X POST "https://your-service.onrender.com/moments/MOMENT_ID/contributions" `
	-H "X-User-Id: user-123" `
	-F "captured_at=2026-09-20T12:00:00Z" `
	-F "consent_to_reconstruct=true" `
	-F "file=@recording.webm;type=video/webm"
```

Uploaded files are returned with a `media_url`. Configure `MOMENT_MEDIA_DIRECTORY`
and `MOMENT_MAX_MEDIA_BYTES` to change local storage and the default 250 MB limit.

## Deploy on Render

Create a Render Web Service connected to this repository and use Docker as the environment. Render will detect the root `Dockerfile` automatically. Set the health check path to `/health`.

The current deployment uses SQLite and local media storage, which are suitable for a demo but not durable on Render's ephemeral filesystem. Use a Render persistent disk or migrate metadata to Postgres and media to S3/R2 before production use.

## Authentication for this MVP

Pass an arbitrary stable user identifier via `X-User-Id`. This is deliberately a development placeholder; replace `current_user` with JWT/OAuth verification before deployment.

## Processing lifecycle

`draft` → `collecting` → `processing` → `ready` (or `failed`). Upload completion moves a moment to `collecting`; calling the processing endpoint starts a simulated reconstruction job. A real worker should consume that job and call the status endpoint after feature matching, alignment, and reconstruction complete.

Start processing only after at least one contribution has returned `201`:

```powershell
curl.exe -X PATCH "https://your-service.onrender.com/moments/MOMENT_ID/processing" `
	-H "X-User-Id: user-123" `
	-H "Content-Type: application/json" `
	-d '{"state":"processing"}'
```

The API also accepts `status` in place of `state`. Do not send `collecting` to this endpoint; uploads set that state automatically.

