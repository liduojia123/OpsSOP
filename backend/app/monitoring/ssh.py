"""SSH 只读诊断执行。

- 私钥加密保存在数据库,只在内存中加载,不落临时文件;
- 连接前校验主机指纹(首次由管理员测试连接获取并确认),不匹配直接拒绝;
- 只执行数据库中的内置模板命令,参数已强校验,不接受任意命令;
- 输出截断 2KB 并脱敏,异常消息不包含密钥。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import time
from dataclasses import dataclass

import paramiko

from app.ops.common import redact_text

MAX_OUTPUT_CHARS = 2000
DEFAULT_TIMEOUT = 15


class SshError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SshTarget:
    host: str
    port: int
    username: str
    private_key_pem: str
    host_key_fingerprint: str = ""


def fingerprint_of(key: paramiko.PKey) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def load_private_key(pem: str) -> paramiko.PKey:
    if not (pem or "").strip():
        raise SshError("config", "未配置 SSH 私钥")
    source = io.StringIO(pem)
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        source.seek(0)
        try:
            return cls.from_private_key(source)
        except Exception:  # noqa: BLE001 - 继续尝试其他类型
            continue
    raise SshError("config", "SSH 私钥格式不支持(支持 Ed25519/RSA/ECDSA PEM)")


class _FingerprintPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, expected: str):
        self.expected = expected

    def missing_host_key(self, client, hostname, key):
        actual = fingerprint_of(key)
        if not self.expected:
            raise SshError("host_key_missing", "服务器未记录主机指纹,请先测试并确认")
        if not hmac.compare_digest(actual, self.expected):
            raise SshError("host_key_mismatch", "服务器主机指纹与记录不一致,已拒绝连接")


def _connect(target: SshTarget, *, policy, timeout: float) -> paramiko.SSHClient:
    key = load_private_key(target.private_key_pem)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(policy)
    try:
        client.connect(
            target.host,
            port=target.port,
            username=target.username,
            pkey=key,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
    except SshError:
        client.close()
        raise
    except paramiko.AuthenticationException as exc:
        client.close()
        raise SshError("auth", "SSH 认证失败,请检查用户名与私钥") from exc
    except paramiko.SSHException as exc:
        client.close()
        raise SshError("protocol", f"SSH 协议错误: {type(exc).__name__}") from exc
    except Exception as exc:  # noqa: BLE001 - 网络错误统一映射
        client.close()
        raise SshError("connect", f"无法连接服务器: {type(exc).__name__}") from exc
    return client


def probe_fingerprint(target: SshTarget, timeout: float = 10) -> str:
    """测试连接并返回服务器主机指纹(管理员确认后保存)。"""
    client = _connect(target, policy=paramiko.AutoAddPolicy(), timeout=timeout)
    try:
        key = client.get_transport().get_remote_server_key()
        return fingerprint_of(key)
    finally:
        client.close()


def run_command(target: SshTarget, command: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """执行一条已渲染的只读命令,返回输出与退出码。"""
    started = time.monotonic()
    client = _connect(
        target,
        policy=_FingerprintPolicy(target.host_key_fingerprint),
        timeout=timeout,
    )
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        channel = stdout.channel
        channel.settimeout(timeout)
        out = stdout.read(MAX_OUTPUT_CHARS + 1)
        err = stderr.read(1024)
        try:
            exit_status = channel.recv_exit_status()
        except Exception:  # noqa: BLE001
            exit_status = -1
        text = (out + err).decode("utf-8", "replace")
    except SshError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SshError("exec", f"命令执行失败: {type(exc).__name__}") from exc
    finally:
        client.close()

    truncated = len(text) > MAX_OUTPUT_CHARS
    text = redact_text(text[:MAX_OUTPUT_CHARS])
    return {
        "stdout": text,
        "exit_status": exit_status,
        "truncated": truncated,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }