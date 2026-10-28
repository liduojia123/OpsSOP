"""版本前置条件；调用方先持有记录锁再检查。"""
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status


def require_current_version(expected: str | None, current: datetime) -> None:
    if not expected:
        raise HTTPException(status.HTTP_428_PRECONDITION_REQUIRED, "缺少记录版本，请刷新后重试")
    try:
        parsed = datetime.fromisoformat(expected.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("missing timezone")
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "记录版本格式无效，请刷新后重试")
    if parsed != current:
        raise HTTPException(status.HTTP_409_CONFLICT, "记录已被其他人修改，请保留当前输入并刷新后重试")


def advance_version(record) -> None:
    """持锁写入时保证版本递增，即使服务节点时钟回拨。"""
    record.updated_at = max(datetime.now(timezone.utc), record.updated_at + timedelta(microseconds=1))
