"""管理员备份：查看、手动触发、下载。

文件始终位于 BACKUP_DIR 内，名称按固定格式校验，避免路径穿越。
下载与触发都会写审计记录。
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.backup import BackupBusy, BackupError, list_backups, run_backup, valid_name
from app.config import get_settings
from app.database import get_db
from app.deps import record_audit, require_admin
from app.maintenance import next_run_at
from app.models import User
from app.schemas import BackupConfigOut, BackupOut, BackupRunOut

router = APIRouter(prefix="/api/admin/backups", tags=["backups"])


@router.get("/config", response_model=BackupConfigOut)
def backup_config(_: User = Depends(require_admin)) -> BackupConfigOut:
    settings = get_settings()
    return BackupConfigOut(
        enabled=settings.backup_enabled,
        directory=settings.backup_dir,
        hour=settings.backup_hour,
        retention_days=settings.backup_retention_days,
        audit_retention_days=settings.audit_retention_days,
        next_run=next_run_at(settings.backup_hour).isoformat(timespec="minutes"),
    )


@router.get("", response_model=list[BackupOut])
def list_backup_files(_: User = Depends(require_admin)) -> list[BackupOut]:
    try:
        return [BackupOut(**item) for item in list_backups(get_settings())]
    except BackupError as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc


@router.post("", response_model=BackupRunOut, status_code=status.HTTP_201_CREATED)
def create_backup(
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> BackupRunOut:
    settings = get_settings()
    try:
        result = run_backup(settings, "manual", db)
    except BackupBusy as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except BackupError as exc:
        record_audit(db, user, "backup.failed", "backup", "", {"reason": str(exc)[:200]})
        db.commit()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc
    record_audit(
        db,
        user,
        "backup.created",
        "backup",
        result["name"],
        {"size_bytes": result["size_bytes"], "pruned": result["pruned"], "trigger": "manual"},
    )
    db.commit()
    return BackupRunOut(**{key: result[key] for key in ("name", "created_at", "size_bytes", "sha256", "trigger", "retention_days")}, pruned=result["pruned"])


@router.get("/{name}")
def download_backup(
    name: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> FileResponse:
    if not valid_name(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "备份不存在")
    directory = Path(get_settings().backup_dir).resolve()
    path = (directory / f"{name}.dump").resolve()
    if path.parent != directory or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "备份不存在")
    record_audit(db, user, "backup.download", "backup", name)
    db.commit()
    return FileResponse(path, media_type="application/octet-stream", filename=f"{name}.dump")

