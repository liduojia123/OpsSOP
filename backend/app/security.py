"""密码哈希与会话令牌。密钥只从环境变量读取,不进入日志。"""

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.config import get_settings

ALGORITHM = "HS256"

# bcrypt 只对前 72 字节做有效哈希;超长内容按字节截断会造成不同口令互相验证通过。
# 因此直接拒绝超长口令(按 UTF-8 字节计),与已有哈希兼容(旧哈希不受影响)。
MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    data = password.encode("utf-8")
    if len(data) > MAX_PASSWORD_BYTES:
        raise ValueError(f"密码过长:bcrypt 仅支持前 {MAX_PASSWORD_BYTES} 字节(UTF-8),请缩短密码")
    return bcrypt.hashpw(data, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        data = password.encode("utf-8")
        if len(data) > MAX_PASSWORD_BYTES:
            # 不静默截断:超长口令一律视为不匹配
            return False
        return bcrypt.checkpw(data, password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_session_token(user_id: int, username: str, role: str) -> tuple[str, datetime]:
    settings = get_settings()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.session_expire_hours)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "exp": expires_at,
        "iat": datetime.now(timezone.utc),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)
    return token, expires_at


def decode_session_token(token: str) -> dict | None:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
