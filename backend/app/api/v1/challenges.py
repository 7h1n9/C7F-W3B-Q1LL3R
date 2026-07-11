import hashlib
import secrets
from pathlib import Path, PurePosixPath, PureWindowsPath

from fastapi import APIRouter, Depends, File, Response, UploadFile
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.core.exceptions import DomainError
from app.models.challenge import Challenge, ChallengeAttachment
from app.models.run import SolveRun
from app.schemas.challenge import ChallengeInput, ChallengeRead

router = APIRouter(prefix="/challenges", tags=["challenges"])


def read(item: Challenge) -> ChallengeRead:
    return ChallengeRead.model_validate(
        {
            **item.__dict__,
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
        }
    )


async def require_challenge(challenge_id: str, session: AsyncSession) -> Challenge:
    item = await session.scalar(select(Challenge).where(Challenge.id == challenge_id))
    if not item:
        raise DomainError("CHALLENGE_NOT_FOUND", "Challenge not found.", status_code=404)
    return item


@router.get("")
async def list_challenges(session: AsyncSession = Depends(get_session)) -> dict:
    items = list(
        (await session.scalars(select(Challenge).order_by(Challenge.created_at.desc()))).all()
    )
    return {"data": [read(item) for item in items]}


@router.post("", status_code=201)
async def create_challenge(
    payload: ChallengeInput, session: AsyncSession = Depends(get_session)
) -> dict:
    values = payload.model_dump()
    if values["challenge_type"] == "TRAFFIC_ANALYSIS":
        values["status"] = "DRAFT"
    item = Challenge(**values)
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return {"data": read(item)}


@router.get("/{challenge_id}")
async def get_challenge(challenge_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    return {"data": read(await require_challenge(challenge_id, session))}


@router.put("/{challenge_id}")
async def update_challenge(
    challenge_id: str, payload: ChallengeInput, session: AsyncSession = Depends(get_session)
) -> dict:
    item = await require_challenge(challenge_id, session)
    for key, value in payload.model_dump().items():
        setattr(item, key, value)
    if item.challenge_type == "TRAFFIC_ANALYSIS":
        primary = (
            await session.get(ChallengeAttachment, item.primary_attachment_id)
            if item.primary_attachment_id
            else None
        )
        item.status = "ACTIVE" if primary and primary.kind == "PCAP" else "DRAFT"
    await session.commit()
    await session.refresh(item)
    return {"data": read(item)}


@router.delete("/{challenge_id}", status_code=204)
async def delete_challenge(
    challenge_id: str, session: AsyncSession = Depends(get_session)
) -> Response:
    item = await require_challenge(challenge_id, session)
    if await session.scalar(select(SolveRun.id).where(SolveRun.challenge_id == item.id).limit(1)):
        raise DomainError("CHALLENGE_HAS_RUNS", "Challenges with SolveRun history cannot be deleted.", status_code=409)
    repository_root = Path(__file__).resolve().parents[4]
    attachment_root = (repository_root / "data" / "challenges" / item.id / "attachments").resolve()
    attachments = list(
        (
            await session.scalars(
                select(ChallengeAttachment).where(ChallengeAttachment.challenge_id == item.id)
            )
        ).all()
    )
    item.primary_attachment_id = None
    await session.flush()
    for attachment in attachments:
        raw_path = repository_root / attachment.relative_path
        path = raw_path.resolve()
        if raw_path.is_symlink() or attachment_root not in path.parents:
            raise DomainError("ATTACHMENT_PATH_INVALID", "Attachment storage path is invalid.", status_code=409)
        if path.is_file():
            path.unlink()
    if attachments:
        await session.execute(
            delete(ChallengeAttachment).where(ChallengeAttachment.challenge_id == item.id)
        )
        await session.flush()
    await session.delete(item)
    await session.commit()
    if attachment_root.is_dir():
        try:
            attachment_root.rmdir()
            attachment_root.parent.rmdir()
        except OSError:
            pass
    return Response(status_code=204)


PCAP_EXTENSIONS = {".pcap", ".pcapng", ".cap"}
PCAP_MAGICS = {
    b"\xd4\xc3\xb2\xa1",
    b"\xa1\xb2\xc3\xd4",
    b"\x4d\x3c\xb2\xa1",
    b"\xa1\xb2\x3c\x4d",
    b"\x0a\x0d\x0d\x0a",
}
MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024


def attachment_read(item: ChallengeAttachment) -> dict:
    return {
        "id": item.id,
        "challenge_id": item.challenge_id,
        "kind": item.kind,
        "original_name": item.original_name,
        "mime_type": item.mime_type,
        "size": item.size,
        "sha256": item.sha256,
        "is_primary": item.is_primary,
        "created_at": item.created_at.isoformat(),
    }


def is_pcap(header: bytes, filename: str) -> bool:
    return Path(filename).suffix.lower() in PCAP_EXTENSIONS and header[:4] in PCAP_MAGICS


@router.get("/{challenge_id}/attachments")
async def list_attachments(challenge_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    await require_challenge(challenge_id, session)
    items = list(
        (
            await session.scalars(
                select(ChallengeAttachment)
                .where(ChallengeAttachment.challenge_id == challenge_id)
                .order_by(ChallengeAttachment.created_at)
            )
        ).all()
    )
    return {"data": [attachment_read(item) for item in items]}


@router.post("/{challenge_id}/attachments", status_code=201)
async def upload_attachment(
    challenge_id: str,
    file: UploadFile = File(...),
    is_primary: bool = False,
    session: AsyncSession = Depends(get_session),
) -> dict:
    challenge = await require_challenge(challenge_id, session)
    supplied_name = file.filename or ""
    posix_name = PurePosixPath(supplied_name.replace("\\", "/"))
    windows_name = PureWindowsPath(supplied_name)
    if (
        not supplied_name
        or "/" in supplied_name
        or "\\" in supplied_name
        or posix_name.is_absolute()
        or windows_name.is_absolute()
        or ".." in posix_name.parts
        or ".." in windows_name.parts
    ):
        raise DomainError("ATTACHMENT_NAME_INVALID", "Attachment name is invalid.", status_code=422)
    original_name = supplied_name
    root = (
        Path(__file__).resolve().parents[4] / "data" / "challenges" / challenge.id / "attachments"
    ).resolve()
    root.mkdir(parents=True, exist_ok=True)
    stored_name = f"{secrets.token_hex(16)}{Path(original_name).suffix.lower()}"
    target = (root / stored_name).resolve()
    if target.parent != root or target.is_symlink():
        raise DomainError(
            "ATTACHMENT_PATH_INVALID", "Attachment storage path is invalid.", status_code=422
        )
    temporary = target.with_suffix(target.suffix + ".upload")
    hasher, size, first = hashlib.sha256(), 0, b""
    try:
        with temporary.open("xb") as handle:
            while chunk := await file.read(64 * 1024):
                size += len(chunk)
                if size > MAX_ATTACHMENT_BYTES:
                    raise DomainError(
                        "ATTACHMENT_TOO_LARGE",
                        "Attachment exceeds the size limit.",
                        status_code=413,
                    )
                if len(first) < 8:
                    first += chunk[: 8 - len(first)]
                hasher.update(chunk)
                handle.write(chunk)
        kind = (
            "PCAP"
            if is_pcap(first, original_name)
            else "SOURCE"
            if Path(original_name).suffix.lower() in {".py", ".js", ".php", ".zip"}
            else "OTHER"
        )
        if challenge.challenge_type == "TRAFFIC_ANALYSIS" and kind != "PCAP":
            raise DomainError(
                "PCAP_REQUIRED",
                "Traffic-analysis attachments must be valid PCAP/PCAPNG files.",
                status_code=422,
            )
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    if is_primary:
        for existing in list(
            (
                await session.scalars(
                    select(ChallengeAttachment).where(
                        ChallengeAttachment.challenge_id == challenge.id
                    )
                )
            ).all()
        ):
            existing.is_primary = False
    relative = target.relative_to(Path(__file__).resolve().parents[4]).as_posix()
    item = ChallengeAttachment(
        challenge_id=challenge.id,
        kind=kind,
        original_name=original_name,
        stored_name=stored_name,
        relative_path=relative,
        mime_type=file.content_type or "application/octet-stream",
        size=size,
        sha256=hasher.hexdigest(),
        is_primary=is_primary
        or (challenge.challenge_type == "TRAFFIC_ANALYSIS" and not challenge.primary_attachment_id),
    )
    session.add(item)
    await session.flush()
    if item.is_primary:
        challenge.primary_attachment_id = item.id
        challenge.status = "ACTIVE" if item.kind == "PCAP" else challenge.status
    await session.commit()
    await session.refresh(item)
    return {"data": attachment_read(item)}


@router.delete("/{challenge_id}/attachments/{attachment_id}", status_code=204)
async def delete_attachment(
    challenge_id: str, attachment_id: str, session: AsyncSession = Depends(get_session)
) -> Response:
    challenge = await require_challenge(challenge_id, session)
    item = await session.get(ChallengeAttachment, attachment_id)
    if not item or item.challenge_id != challenge.id:
        raise DomainError("ATTACHMENT_NOT_FOUND", "Attachment not found.", status_code=404)
    repository_root = Path(__file__).resolve().parents[4]
    attachment_root = (
        repository_root
        / "data"
        / "challenges"
        / challenge.id
        / "attachments"
    ).resolve()
    raw_path = repository_root / item.relative_path
    path = raw_path.resolve()
    if attachment_root not in path.parents or raw_path.is_symlink():
        raise DomainError("ATTACHMENT_PATH_INVALID", "Attachment storage path is invalid.", status_code=409)
    if path.is_file():
        path.unlink()
    if challenge.primary_attachment_id == item.id:
        challenge.primary_attachment_id = None
        if challenge.challenge_type == "TRAFFIC_ANALYSIS":
            challenge.status = "DRAFT"
    await session.delete(item)
    await session.commit()
    return Response(status_code=204)
