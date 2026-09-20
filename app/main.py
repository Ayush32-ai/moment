from __future__ import annotations

import os
import base64
import hashlib
import hmac
import json
import re
import shutil
import sqlite3
import secrets
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Generator
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import AliasChoices, BaseModel, Field


DATABASE_PATH = Path(os.getenv("MOMENT_DATABASE_PATH", "moment.db"))
MEDIA_DIRECTORY = Path(os.getenv("MOMENT_MEDIA_DIRECTORY", "media"))
MAX_MEDIA_BYTES = int(os.getenv("MOMENT_MAX_MEDIA_BYTES", str(250 * 1024 * 1024)))
AUTH_SECRET = os.getenv("MOMENT_AUTH_SECRET", "")
AUTH_ALLOW_LEGACY_HEADER = os.getenv("MOMENT_ALLOW_LEGACY_AUTH", "false").lower() == "true"
TOKEN_TTL_SECONDS = int(os.getenv("MOMENT_TOKEN_TTL_SECONDS", str(7 * 24 * 60 * 60)))
ALLOWED_MEDIA_TYPES = {"image", "video", "audio"}
app = FastAPI(title="Moment API", version="0.1.0", description="Shared-experience reconstruction MVP")


class Visibility(str, Enum):
    private = "private"
    friends = "friends"
    participants = "participants"
    public = "public"


class MomentState(str, Enum):
    draft = "draft"
    collecting = "collecting"
    processing = "processing"
    ready = "ready"
    failed = "failed"


class MomentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    visibility: Visibility = Visibility.participants
    starts_at: datetime | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=30, pattern=r"^[A-Za-z0-9_.-]+$")
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str | None = Field(default=None, min_length=3, max_length=320)
    email: str | None = Field(default=None, min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)


class AuthOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_seconds: int
    user_id: str
    username: str
    email: str


class MomentOut(MomentCreate):
    id: str
    owner_id: str
    state: MomentState
    created_at: datetime


class UploadIntentCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(pattern=r"^(video|audio|image)/[a-zA-Z0-9.+-]+$")
    captured_at: datetime
    duration_ms: int | None = Field(default=None, ge=0)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    device_timestamp_ms: int | None = Field(default=None, ge=0)
    consent_to_reconstruct: bool


class UploadIntentOut(BaseModel):
    contribution_id: str
    upload_url: str
    upload_headers: dict[str, str]
    expires_in_seconds: int


class ContributionOut(BaseModel):
    id: str
    moment_id: str
    contributor_id: str
    filename: str
    content_type: str
    captured_at: datetime
    duration_ms: int | None
    consent_to_reconstruct: bool
    upload_status: str
    created_at: datetime
    source_type: str | None = None
    source_url: str | None = None
    media_url: str | None = None


class ProcessingUpdate(BaseModel):
    state: MomentState | None = Field(default=None, validation_alias=AliasChoices("state", "status"))
    message: str | None = Field(default=None, max_length=500)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@contextmanager
def connection() -> Generator[sqlite3.Connection, None, None]:
    db = sqlite3.connect(DATABASE_PATH)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    finally:
        db.close()


def initialize_database() -> None:
    with connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS moments (
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, title TEXT NOT NULL,
                visibility TEXT NOT NULL, starts_at TEXT, latitude REAL, longitude REAL,
                state TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS contributions (
                id TEXT PRIMARY KEY, moment_id TEXT NOT NULL, contributor_id TEXT NOT NULL,
                filename TEXT NOT NULL, content_type TEXT NOT NULL, captured_at TEXT NOT NULL,
                duration_ms INTEGER, latitude REAL, longitude REAL, device_timestamp_ms INTEGER,
                consent_to_reconstruct INTEGER NOT NULL, upload_status TEXT NOT NULL,
                media_path TEXT, source_type TEXT NOT NULL DEFAULT 'file', source_url TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(moment_id) REFERENCES moments(id)
            );
            """
        )
        user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "username" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN username TEXT")
            existing_users = db.execute("SELECT id, email FROM users").fetchall()
            used_usernames: set[str] = set()
            for existing in existing_users:
                base = re.sub(r"[^a-z0-9_.-]", "", existing["email"].split("@", 1)[0].lower()) or "user"
                base = base[:24]
                username = base
                suffix = 1
                while username in used_usernames or len(username) < 3:
                    username = f"{base[:24 - len(str(suffix))]}{suffix}"
                    suffix += 1
                used_usernames.add(username)
                db.execute("UPDATE users SET username = ? WHERE id = ?", (username, existing["id"]))
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username)")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(contributions)")}
        if "media_path" not in columns:
            db.execute("ALTER TABLE contributions ADD COLUMN media_path TEXT")
        if "source_type" not in columns:
            db.execute("ALTER TABLE contributions ADD COLUMN source_type TEXT NOT NULL DEFAULT 'file'")
        if "source_url" not in columns:
            db.execute("ALTER TABLE contributions ADD COLUMN source_url TEXT")
    MEDIA_DIRECTORY.mkdir(parents=True, exist_ok=True)


@app.on_event("startup")
def startup() -> None:
    if not AUTH_SECRET:
        raise RuntimeError("MOMENT_AUTH_SECRET must be configured")
    initialize_database()


def normalized_email(email: str) -> str:
    return email.strip().lower()


def normalized_username(username: str) -> str:
    return username.strip().lower()


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256$310000${salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (TypeError, ValueError):
        return False


def encode_token(user_id: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"sub": user_id, "exp": int(time.time()) + TOKEN_TTL_SECONDS}

    def encode(value: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=").decode()

    signing_input = f"{encode(header)}.{encode(payload)}"
    signature = hmac.new(AUTH_SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def decode_token(token: str) -> str:
    try:
        encoded_header, encoded_payload, encoded_signature = token.split(".", 2)
        signing_input = f"{encoded_header}.{encoded_payload}"
        expected = hmac.new(AUTH_SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
        provided = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        if not hmac.compare_digest(expected, provided):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4)))
        if not payload.get("sub") or int(payload.get("exp", 0)) <= int(time.time()):
            raise ValueError
        return str(payload["sub"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired access token")


def current_user(
    authorization: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header()] = None,
) -> str:
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            return decode_token(token)
    if AUTH_ALLOW_LEGACY_HEADER and x_user_id and x_user_id.strip():
        return x_user_id.strip()
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer access token is required")


def moment_from_row(row: sqlite3.Row) -> MomentOut:
    return MomentOut(
        id=row["id"], owner_id=row["owner_id"], title=row["title"], visibility=row["visibility"],
        starts_at=datetime.fromisoformat(row["starts_at"]) if row["starts_at"] else None,
        latitude=row["latitude"], longitude=row["longitude"], state=row["state"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def contribution_from_row(row: sqlite3.Row) -> ContributionOut:
    return ContributionOut(
        id=row["id"], moment_id=row["moment_id"], contributor_id=row["contributor_id"],
        filename=row["filename"], content_type=row["content_type"],
        captured_at=datetime.fromisoformat(row["captured_at"]), duration_ms=row["duration_ms"],
        consent_to_reconstruct=bool(row["consent_to_reconstruct"]), upload_status=row["upload_status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        source_type=row["source_type"], source_url=row["source_url"],
        media_url=f"/v1/contributions/{row['id']}/media" if row["media_path"] else None,
    )


def valid_spotify_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.netloc.lower() in {"open.spotify.com", "spotify.link"}


def safe_filename(filename: str) -> str:
    name = Path(filename).name
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "upload"


def can_access_contribution(row: sqlite3.Row, user_id: str) -> None:
    with connection() as db:
        moment = fetch_moment_or_404(db, row["moment_id"])
    if row["contributor_id"] != user_id and moment["owner_id"] != user_id and moment["visibility"] != Visibility.public.value:
        raise HTTPException(status_code=403, detail="You do not have access to this contribution")


def fetch_moment_or_404(db: sqlite3.Connection, moment_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM moments WHERE id = ?", (moment_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Moment not found")
    return row


def require_owner(row: sqlite3.Row, user_id: str) -> None:
    if row["owner_id"] != user_id:
        raise HTTPException(status_code=403, detail="Only the Moment owner may perform this action")


@app.post("/auth/register", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
@app.post("/v1/auth/register", response_model=AuthOut, status_code=status.HTTP_201_CREATED, include_in_schema=False)
def register(payload: RegisterRequest) -> AuthOut:
    username = normalized_username(payload.username)
    email = normalized_email(payload.email)
    if "@" not in email:
        raise HTTPException(status_code=422, detail="A valid email address is required")
    user_id = str(uuid.uuid4())
    try:
        with connection() as db:
            db.execute(
                "INSERT INTO users (id, username, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, username, email, password_hash(payload.password), iso(utc_now())),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="That username or email is already registered")
    return AuthOut(access_token=encode_token(user_id), expires_in_seconds=TOKEN_TTL_SECONDS, user_id=user_id, username=username, email=email)


@app.post("/auth/login", response_model=AuthOut)
@app.post("/v1/auth/login", response_model=AuthOut, include_in_schema=False)
def login(payload: LoginRequest) -> AuthOut:
    identifier = (payload.username or payload.email or "").strip().lower()
    if not identifier:
        raise HTTPException(status_code=422, detail="Send username or email")
    with connection() as db:
        user = db.execute("SELECT * FROM users WHERE username = ? OR email = ?", (identifier, identifier)).fetchone()
    if not user or not password_matches(payload.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username/email or password")
    return AuthOut(access_token=encode_token(user["id"]), expires_in_seconds=TOKEN_TTL_SECONDS, user_id=user["id"], username=user["username"], email=user["email"])


@app.get("/auth/me", response_model=dict[str, str])
@app.get("/v1/auth/me", response_model=dict[str, str], include_in_schema=False)
def auth_me(user_id: Annotated[str, Depends(current_user)]) -> dict[str, str]:
    with connection() as db:
        user = db.execute("SELECT id, username, email FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        raise HTTPException(status_code=401, detail="User account no longer exists")
    return {"user_id": user["id"], "username": user["username"], "email": user["email"]}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "Moment API", "docs": "/docs", "health": "/health"}


@app.post("/v1/moments", response_model=MomentOut, status_code=status.HTTP_201_CREATED)
@app.post("/moments", response_model=MomentOut, status_code=status.HTTP_201_CREATED, include_in_schema=False)
def create_moment(payload: MomentCreate, user_id: Annotated[str, Depends(current_user)]) -> MomentOut:
    now = utc_now()
    moment = MomentOut(id=str(uuid.uuid4()), owner_id=user_id, state=MomentState.draft, created_at=now, **payload.model_dump())
    with connection() as db:
        db.execute("INSERT INTO moments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            moment.id, moment.owner_id, moment.title, moment.visibility.value,
            iso(moment.starts_at) if moment.starts_at else None, moment.latitude, moment.longitude,
            moment.state.value, iso(moment.created_at),
        ))
    return moment


@app.get("/v1/moments", response_model=list[MomentOut])
@app.get("/moments", response_model=list[MomentOut], include_in_schema=False)
def list_moments(user_id: Annotated[str, Depends(current_user)], limit: int = Query(default=20, ge=1, le=100)) -> list[MomentOut]:
    with connection() as db:
        rows = db.execute("SELECT * FROM moments WHERE owner_id = ? OR visibility = 'public' ORDER BY created_at DESC LIMIT ?", (user_id, limit)).fetchall()
    return [moment_from_row(row) for row in rows]


@app.get("/v1/moments/{moment_id}", response_model=MomentOut)
@app.get("/moments/{moment_id}", response_model=MomentOut, include_in_schema=False)
def get_moment(moment_id: str, user_id: Annotated[str, Depends(current_user)]) -> MomentOut:
    with connection() as db:
        row = fetch_moment_or_404(db, moment_id)
        if row["owner_id"] != user_id and row["visibility"] != Visibility.public.value:
            raise HTTPException(status_code=403, detail="You do not have access to this Moment")
        return moment_from_row(row)


@app.post("/v1/moments/{moment_id}/contributions/upload-intents", response_model=UploadIntentOut, status_code=status.HTTP_201_CREATED)
@app.post("/moments/{moment_id}/contributions/upload-intents", response_model=UploadIntentOut, status_code=status.HTTP_201_CREATED, include_in_schema=False)
def create_upload_intent(moment_id: str, payload: UploadIntentCreate, user_id: Annotated[str, Depends(current_user)]) -> UploadIntentOut:
    if not payload.consent_to_reconstruct:
        raise HTTPException(status_code=422, detail="Explicit reconstruction consent is required")
    contribution_id = str(uuid.uuid4())
    now = utc_now()
    with connection() as db:
        moment = fetch_moment_or_404(db, moment_id)
        if moment["state"] in (MomentState.ready.value, MomentState.failed.value):
            raise HTTPException(status_code=409, detail="This Moment is no longer accepting contributions")
        db.execute("""INSERT INTO contributions
            (id, moment_id, contributor_id, filename, content_type, captured_at, duration_ms,
             latitude, longitude, device_timestamp_ms, consent_to_reconstruct, upload_status,
             media_path, source_type, source_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            contribution_id, moment_id, user_id, payload.filename, payload.content_type, iso(payload.captured_at),
            payload.duration_ms, payload.latitude, payload.longitude, payload.device_timestamp_ms, 1, "pending_upload",
            None, "file", None, iso(now),
        ))
    return UploadIntentOut(contribution_id=contribution_id, upload_url=f"/v1/contributions/{contribution_id}/media", upload_headers={"Content-Type": payload.content_type}, expires_in_seconds=900)


@app.post("/v1/moments/{moment_id}/contributions", response_model=ContributionOut, status_code=status.HTTP_201_CREATED)
@app.post("/moments/{moment_id}/contributions", response_model=ContributionOut, status_code=status.HTTP_201_CREATED, include_in_schema=False)
async def upload_contribution(
    moment_id: str,
    user_id: Annotated[str, Depends(current_user)],
    captured_at: datetime = Form(...),
    consent_to_reconstruct: bool = Form(...),
    file: UploadFile | None = File(default=None),
    spotify_url: str | None = Form(default=None),
    duration_ms: int | None = Form(default=None),
    latitude: float | None = Form(default=None),
    longitude: float | None = Form(default=None),
    device_timestamp_ms: int | None = Form(default=None),
) -> ContributionOut:
    if not consent_to_reconstruct:
        raise HTTPException(status_code=422, detail="Explicit reconstruction consent is required")
    if (file is None) == (spotify_url is None):
        raise HTTPException(status_code=422, detail="Provide exactly one media file or Spotify URL")
    if spotify_url and not valid_spotify_url(spotify_url):
        raise HTTPException(status_code=422, detail="spotify_url must be an HTTPS Spotify track URL")

    now = utc_now()
    contribution_id = str(uuid.uuid4())
    media_path: Path | None = None
    filename = "spotify-track"
    content_type = "audio/spotify"
    source_type = "spotify"

    with connection() as db:
        moment = fetch_moment_or_404(db, moment_id)
        if moment["state"] in (MomentState.ready.value, MomentState.failed.value):
            raise HTTPException(status_code=409, detail="This Moment is no longer accepting contributions")

        if file is not None:
            media_group = (file.content_type or "").split("/", 1)[0]
            if media_group not in ALLOWED_MEDIA_TYPES:
                raise HTTPException(status_code=415, detail="Only image, video, and audio files are supported")
            filename = safe_filename(file.filename or "upload")
            content_type = file.content_type or "application/octet-stream"
            media_path = MEDIA_DIRECTORY / f"{contribution_id}-{filename}"
            total_bytes = 0
            try:
                with media_path.open("wb") as destination:
                    while chunk := await file.read(1024 * 1024):
                        total_bytes += len(chunk)
                        if total_bytes > MAX_MEDIA_BYTES:
                            raise HTTPException(status_code=413, detail="Media file is too large")
                        destination.write(chunk)
            except Exception:
                if media_path.exists():
                    media_path.unlink()
                raise
            await file.close()
            source_type = media_group

        db.execute("""INSERT INTO contributions
            (id, moment_id, contributor_id, filename, content_type, captured_at, duration_ms,
             latitude, longitude, device_timestamp_ms, consent_to_reconstruct, upload_status,
             media_path, source_type, source_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            contribution_id, moment_id, user_id, filename, content_type, iso(captured_at), duration_ms,
            latitude, longitude, device_timestamp_ms, 1, "uploaded" if media_path else "linked",
            str(media_path) if media_path else None, source_type, spotify_url, iso(now),
        ))
        db.execute("UPDATE moments SET state = 'collecting' WHERE id = ? AND state = 'draft'", (moment_id,))
        row = db.execute("SELECT * FROM contributions WHERE id = ?", (contribution_id,)).fetchone()
    return contribution_from_row(row)


@app.get("/v1/contributions/{contribution_id}/media")
@app.get("/contributions/{contribution_id}/media", include_in_schema=False)
def get_contribution_media(contribution_id: str, user_id: Annotated[str, Depends(current_user)]) -> FileResponse:
    with connection() as db:
        row = db.execute("SELECT * FROM contributions WHERE id = ?", (contribution_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Contribution not found")
    can_access_contribution(row, user_id)
    if not row["media_path"] or not Path(row["media_path"]).is_file():
        raise HTTPException(status_code=404, detail="This contribution is a Spotify link, not an uploaded file")
    return FileResponse(row["media_path"], media_type=row["content_type"], filename=row["filename"])


@app.put("/v1/contributions/{contribution_id}/media", response_model=None, status_code=status.HTTP_204_NO_CONTENT)
@app.put("/contributions/{contribution_id}/media", response_model=None, status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
async def put_contribution_media(
    contribution_id: str,
    request: Request,
    user_id: Annotated[str, Depends(current_user)],
) -> None:
    with connection() as db:
        row = db.execute("SELECT * FROM contributions WHERE id = ?", (contribution_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Contribution not found")
        if row["contributor_id"] != user_id:
            raise HTTPException(status_code=403, detail="Only the contributor may upload this contribution")
        if row["source_type"] != "file":
            raise HTTPException(status_code=409, detail="This contribution is not a file upload")

        media_path = MEDIA_DIRECTORY / f"{contribution_id}-{safe_filename(row['filename'])}"
        total_bytes = 0
        try:
            with media_path.open("wb") as destination:
                async for chunk in request.stream():
                    total_bytes += len(chunk)
                    if total_bytes > MAX_MEDIA_BYTES:
                        raise HTTPException(status_code=413, detail="Media file is too large")
                    destination.write(chunk)
        except Exception:
            if media_path.exists():
                media_path.unlink()
            raise
        db.execute("UPDATE contributions SET media_path = ?, upload_status = 'uploaded' WHERE id = ?", (str(media_path), contribution_id))
        db.execute("UPDATE moments SET state = 'collecting' WHERE id = ? AND state = 'draft'", (row["moment_id"],))


@app.post("/v1/contributions/{contribution_id}/complete", response_model=ContributionOut)
@app.post("/contributions/{contribution_id}/complete", response_model=ContributionOut, include_in_schema=False)
def complete_upload(contribution_id: str, user_id: Annotated[str, Depends(current_user)]) -> ContributionOut:
    with connection() as db:
        row = db.execute("SELECT * FROM contributions WHERE id = ?", (contribution_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Contribution not found")
        if row["contributor_id"] != user_id:
            raise HTTPException(status_code=403, detail="Only the contributor may complete this upload")
        db.execute("UPDATE contributions SET upload_status = 'uploaded' WHERE id = ?", (contribution_id,))
        db.execute("UPDATE moments SET state = 'collecting' WHERE id = ? AND state = 'draft'", (row["moment_id"],))
        updated = db.execute("SELECT * FROM contributions WHERE id = ?", (contribution_id,)).fetchone()
    return contribution_from_row(updated)


@app.get("/v1/moments/{moment_id}/contributions", response_model=list[ContributionOut])
@app.get("/moments/{moment_id}/contributions", response_model=list[ContributionOut], include_in_schema=False)
def list_contributions(moment_id: str, user_id: Annotated[str, Depends(current_user)]) -> list[ContributionOut]:
    with connection() as db:
        moment = fetch_moment_or_404(db, moment_id)
        if moment["owner_id"] != user_id and moment["visibility"] != Visibility.public.value:
            raise HTTPException(status_code=403, detail="You do not have access to this Moment")
        rows = db.execute("SELECT * FROM contributions WHERE moment_id = ? AND consent_to_reconstruct = 1 ORDER BY captured_at", (moment_id,)).fetchall()
    return [contribution_from_row(row) for row in rows]


@app.delete("/v1/contributions/{contribution_id}", response_model=None, status_code=status.HTTP_204_NO_CONTENT)
@app.delete("/contributions/{contribution_id}", response_model=None, status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
def withdraw_contribution(contribution_id: str, user_id: Annotated[str, Depends(current_user)]) -> None:
    with connection() as db:
        row = db.execute("SELECT * FROM contributions WHERE id = ?", (contribution_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Contribution not found")
        if row["contributor_id"] != user_id:
            raise HTTPException(status_code=403, detail="Only the contributor may withdraw this contribution")
        db.execute("DELETE FROM contributions WHERE id = ?", (contribution_id,))


@app.patch("/v1/moments/{moment_id}/processing", response_model=MomentOut)
@app.patch("/moments/{moment_id}/processing", response_model=MomentOut, include_in_schema=False)
def update_processing(moment_id: str, payload: ProcessingUpdate, user_id: Annotated[str, Depends(current_user)]) -> MomentOut:
    with connection() as db:
        row = fetch_moment_or_404(db, moment_id)
        require_owner(row, user_id)
        if payload.state is None:
            raise HTTPException(status_code=422, detail="Send JSON with state: processing, ready, or failed")
        if payload.state not in (MomentState.processing, MomentState.ready, MomentState.failed):
            raise HTTPException(status_code=422, detail="Processing state must be processing, ready, or failed; collecting is set by upload")
        if payload.state == MomentState.processing:
            uploaded = db.execute("SELECT 1 FROM contributions WHERE moment_id = ? AND upload_status = 'uploaded' LIMIT 1", (moment_id,)).fetchone()
            if not uploaded:
                raise HTTPException(status_code=409, detail="Upload at least one contribution before processing")
        db.execute("UPDATE moments SET state = ? WHERE id = ?", (payload.state.value, moment_id))
        updated = fetch_moment_or_404(db, moment_id)
    return moment_from_row(updated)
