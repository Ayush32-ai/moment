from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Generator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, Field


DATABASE_PATH = Path(os.getenv("MOMENT_DATABASE_PATH", "moment.db"))
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


class ProcessingUpdate(BaseModel):
    state: MomentState
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
            CREATE TABLE IF NOT EXISTS contributions (
                id TEXT PRIMARY KEY, moment_id TEXT NOT NULL, contributor_id TEXT NOT NULL,
                filename TEXT NOT NULL, content_type TEXT NOT NULL, captured_at TEXT NOT NULL,
                duration_ms INTEGER, latitude REAL, longitude REAL, device_timestamp_ms INTEGER,
                consent_to_reconstruct INTEGER NOT NULL, upload_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(moment_id) REFERENCES moments(id)
            );
            """
        )


@app.on_event("startup")
def startup() -> None:
    initialize_database()


def current_user(x_user_id: Annotated[str | None, Header()] = None) -> str:
    if not x_user_id or not x_user_id.strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="X-User-Id header is required")
    return x_user_id.strip()


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
    )


def fetch_moment_or_404(db: sqlite3.Connection, moment_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM moments WHERE id = ?", (moment_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Moment not found")
    return row


def require_owner(row: sqlite3.Row, user_id: str) -> None:
    if row["owner_id"] != user_id:
        raise HTTPException(status_code=403, detail="Only the Moment owner may perform this action")


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
        db.execute("""INSERT INTO contributions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            contribution_id, moment_id, user_id, payload.filename, payload.content_type, iso(payload.captured_at),
            payload.duration_ms, payload.latitude, payload.longitude, payload.device_timestamp_ms, 1,
            "pending_upload", iso(now),
        ))
    # Replace this development URL with a short-lived S3/R2 presigned PUT URL in production.
    return UploadIntentOut(contribution_id=contribution_id, upload_url=f"https://storage.example.invalid/moment/{moment_id}/{contribution_id}", upload_headers={"Content-Type": payload.content_type}, expires_in_seconds=900)


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
        if payload.state not in (MomentState.processing, MomentState.ready, MomentState.failed):
            raise HTTPException(status_code=422, detail="Processing state must be processing, ready, or failed")
        if payload.state == MomentState.processing:
            uploaded = db.execute("SELECT 1 FROM contributions WHERE moment_id = ? AND upload_status = 'uploaded' LIMIT 1", (moment_id,)).fetchone()
            if not uploaded:
                raise HTTPException(status_code=409, detail="Upload at least one contribution before processing")
        db.execute("UPDATE moments SET state = ? WHERE id = ?", (payload.state.value, moment_id))
        updated = fetch_moment_or_404(db, moment_id)
    return moment_from_row(updated)
