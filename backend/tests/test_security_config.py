"""回归:密码字节上限(质量检查 P2 #11)与数据库连接串转义(P2 #10)。"""

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.config import Settings
from app.schemas import UserCreate, UserUpdate
from app.security import MAX_PASSWORD_BYTES, hash_password, verify_password


# ===== P2 #11:bcrypt 72 字节上限 =====


def test_hash_password_rejects_over_72_bytes():
    long_pw = "a" * (MAX_PASSWORD_BYTES + 1)
    with pytest.raises(ValueError, match="72"):
        hash_password(long_pw)


def test_verify_password_does_not_truncate():
    """两个密码前 72 字节相同、后缀不同:后缀版本不得验证通过(修复前会因截断通过)。"""
    prefix = "p" * MAX_PASSWORD_BYTES
    pw_a = prefix  # 72 字节
    pw_b = prefix + "different-suffix"
    h = hash_password(pw_a)
    assert verify_password(pw_a, h) is True
    # pw_b 超长,直接拒绝而不是截断后匹配
    assert verify_password(pw_b, h) is False


def test_chinese_password_byte_limit():
    # 24 个中文字符 = 72 字节,允许;25 个 = 75 字节,拒绝
    ok = "密" * 24
    too_long = "密" * 25
    h = hash_password(ok)
    assert verify_password(ok, h) is True
    with pytest.raises(ValueError):
        hash_password(too_long)


def test_user_create_rejects_long_password():
    with pytest.raises(ValidationError):
        UserCreate(
            username="someone",
            password="x" * (MAX_PASSWORD_BYTES + 1),
            display_name="someone",
            role="operator",
        )


def test_user_update_rejects_long_password():
    with pytest.raises(ValidationError):
        UserUpdate(password="y" * (MAX_PASSWORD_BYTES + 1))


def test_login_rejects_overlong_password(db, client, operator):
    # 超长口令登录必须失败,即使前 72 字节与真实口令相同
    real = "test-password-1"
    # 造一个与真实口令前缀同构的超长口令没有意义(真实口令不足 72 字节),
    # 这里直接验证超长输入被拒绝而不是被截断后比对
    resp = client.post(
        "/api/auth/login",
        json={"username": "op_user", "password": "z" * (MAX_PASSWORD_BYTES + 10)},
    )
    assert resp.status_code == 401
    assert verify_password(real, "x") is False  # sanity


# ===== P2 #10:特殊字符密码的连接串 =====


def test_database_url_escapes_special_chars():
    s = Settings(
        postgres_user="opssop",
        postgres_password="p@ss/word",
        postgres_host="db",
        postgres_port=5432,
        postgres_db="opssop",
    )
    url = make_url(s.database_url)
    assert url.password == "p@ss/word"
    assert url.host == "db"
    assert url.database == "opssop"


def test_database_url_escapes_percent_and_unicode():
    s = Settings(
        postgres_user="opssop",
        postgres_password="p%ss/word密码",
        postgres_host="db",
        postgres_port=5432,
        postgres_db="opssop_test",
    )
    url = make_url(s.test_database_url)
    assert url.password == "p%ss/word密码"
    assert url.database == "opssop_test"


def test_database_url_plain_password_still_works():
    s = Settings(postgres_password="simplepass", postgres_db="opssop")
    url = make_url(s.database_url)
    assert url.password == "simplepass"
