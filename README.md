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
The request must include an `Authorization: Bearer ACCESS_TOKEN` header, `captured_at`, and
`consent_to_reconstruct=true`. Provide exactly one of:

	- `file`: an image, video, or audio file. Browser camera and microphone recordings can be sent as `Blob` or `File` objects.
- `spotify_url`: an HTTPS Spotify track URL. The API stores the link and does not download or copy Spotify audio.

Example file upload:

```powershell
curl.exe -X POST "https://your-service.onrender.com/moments/MOMENT_ID/contributions" `
	-H "Authorization: Bearer ACCESS_TOKEN" `
	-F "captured_at=2026-09-20T12:00:00Z" `
	-F "consent_to_reconstruct=true" `
	-F "file=@recording.webm;type=video/webm"
```

Uploaded files are returned with a `media_url`. Configure `MOMENT_MEDIA_DIRECTORY`
and `MOMENT_MAX_MEDIA_BYTES` to change local storage and the default 250 MB limit.

## Deploy on Render

Create a Render Web Service connected to this repository and use Docker as the environment. Render will detect the root `Dockerfile` automatically. Set the health check path to `/health`.

The current deployment uses SQLite and local media storage, which are suitable for a demo but not durable on Render's ephemeral filesystem. Without a Render persistent disk, a restart or redeploy can erase user accounts, causing valid credentials to return `401 Invalid username/email or password`. The app creates missing storage directories and falls back to `/app` if `/var/data` is unavailable, so the service can still boot, but that fallback is not durable. Use a Render persistent disk or migrate metadata to Postgres and media to S3/R2 before production use.

## Authentication

Set `MOMENT_AUTH_SECRET` in Render and local environments to a long random value.
The service can boot without it for a demo, but generated tokens stop working
after every restart; production deployments must configure this variable.
The API provides `POST /auth/register`, `POST /auth/login`, and `GET /auth/me`.
Registration and login accept JSON:

```json
{"username":"ayush","email":"you@example.com","password":"at-least-8-characters"}
```

`/signup` and `/register` are aliases for `/auth/register`; `/login` is an
alias for `/auth/login`. Send JSON with `Content-Type: application/json`, not
form data.

The registration and login responses include `username`. You can also find the
signed-in user's username at `GET /auth/me`; send the access token in the
`Authorization: Bearer ACCESS_TOKEN` header. Login accepts either
`{"username":"ayush","password":"..."}` or
`{"email":"you@example.com","password":"..."}`.

Use the returned `access_token` on all protected requests:

```powershell
curl.exe "https://your-service.onrender.com/moments" `
	-H "Authorization: Bearer ACCESS_TOKEN"
```

For a short migration period, set `MOMENT_ALLOW_LEGACY_AUTH=true` to allow the
old `X-User-Id` header. Disable it after clients switch to bearer tokens.
Tokens expire after 30 days by default; configure
`MOMENT_TOKEN_TTL_SECONDS` if needed. Passwords are stored as salted PBKDF2
hashes and are never returned by the API.

Render must have these environment variables configured:

```text
MOMENT_AUTH_SECRET=<stable-long-random-value>
MOMENT_DATABASE_PATH=/var/data/moment.db
MOMENT_MEDIA_DIRECTORY=/var/data/media
MOMENT_TOKEN_TTL_SECONDS=2592000
```

Attach a Render persistent disk mounted at `/var/data`. A stable auth secret
prevents tokens becoming invalid after restarts, and the persistent disk keeps
registered accounts and uploaded media available after redeploys.

## Spotify Developer search

Create an app in the Spotify Developer Dashboard and add these Render
environment variables:

```text
SPOTIFY_CLIENT_ID=your-client-id
SPOTIFY_CLIENT_SECRET=your-client-secret
```

After signing in, search tracks with:

```powershell
curl.exe "https://your-service.onrender.com/spotify/search?query=Daft%20Punk%20Instant%20Crush" `
	-H "Authorization: Bearer ACCESS_TOKEN"
```

The endpoint returns track metadata and `spotify_url`. Send that URL as the
`spotify_url` field when adding the track to a Moment. The service does not
download, copy, or stream Spotify audio; playback remains on Spotify.

## Processing lifecycle

`draft` → `collecting` → `processing` → `ready` (or `failed`). Upload completion moves a moment to `collecting`; calling the processing endpoint starts a simulated reconstruction job. A real worker should consume that job and call the status endpoint after feature matching, alignment, and reconstruction complete.

Start processing only after at least one contribution has returned `201`:

```powershell
curl.exe -X PATCH "https://your-service.onrender.com/moments/MOMENT_ID/processing" `
	-H "Authorization: Bearer ACCESS_TOKEN" `
	-H "Content-Type: application/json" `
	-d '{"state":"processing"}'
```

The API also accepts `status` in place of `state`. Do not send `collecting` to this endpoint; uploads set that state automatically.

