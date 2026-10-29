"""服务端脱敏:发送给模型前对常见敏感信息打码。

说明:规则无法覆盖所有敏感信息(如业务内部域名、非标准自定义凭据格式),
脱敏只是纵深防御的一层;local 模式与 LOCAL_ONLY 才是数据不出网的边界。

覆盖常见日志形态(不识别任意编码):
- JSON/结构化日志:`{"password":"x"}`、`"Authorization": "Bearer x"` 等带引号键;
- message 内二次序列化的转义 JSON:`{\"password\":\"x\"}`(键的引号带反斜杠);
  转义引号值为完整定界,值内含逗号/分号/右花括号不提前终止(`\"password\":\"a,b\"`);
- JSON unicode 转义键:`{"pass\\u0077ord":"x"}`;键内空白:`{" password ":"x"}`;
- 带引号值(允许 \\' \\" 转义):`password='a \\'b\\''`、`"token": "a \\"b\\""`;
- bash 引号拼接:`password='a'\\''b'`;
- 无引号键值(值到逗号/分号/} 或行尾,允许含空格):`password=my secret value`;
- PEM 私钥块(含只有 BEGIN、缺 END 的截断形态,保守打码到结尾)、JWT、
  AWS Access Key、URL 内嵌凭据、长十六进制密钥。
"""

import re
from typing import Any

REDACTED = "[REDACTED]"


def _letter(c: str) -> str:
    """键名字母允许原样或 JSON \\u00XX 转义(如 pass\\u0077ord);大小写转义各覆盖。"""
    lo, hi = ord(c.lower()), ord(c.upper())
    return rf"(?:{re.escape(c)}|\\[uU]00{lo:02x}|\\[uU]00{hi:02x})"


def _key_pat(word: str) -> str:
    """按字符展开键名:字母允许 unicode 转义写法,分隔符原样。"""
    return "".join(_letter(ch) if ch.isalpha() else re.escape(ch) for ch in word)


# 敏感键名(与常见变体;键名可被 \\u00XX 逐字母转义)
_KEY_ALT = "|".join(
    [
        _key_pat("password"),
        _key_pat("passwd"),
        _key_pat("pwd"),
        _key_pat("token"),
        _key_pat("secret"),
        rf"{_key_pat('api')}[_-]?{_key_pat('key')}",
        rf"{_key_pat('access')}[_-]?{_key_pat('key')}",
        rf"{_key_pat('private')}[_-]?{_key_pat('key')}",
        rf"{_key_pat('client')}[_-]?{_key_pat('secret')}",
        rf"{_key_pat('session')}[_-]?{_key_pat('id')}",
        _key_pat("authorization"),
        rf"{_key_pat('access')}[_-]?{_key_pat('token')}",
        rf"{_key_pat('refresh')}[_-]?{_key_pat('token')}",
        rf"{_key_pat('auth')}[_-]?{_key_pat('token')}",
    ]
)
# 带引号键:允许键内空白、引号外层反斜杠转义(\"password\")与键名 unicode 转义
_KEY_Q = rf"(?:\\*[\"'])\s*(?:{_KEY_ALT})\s*(?:\\*[\"'])"
# 无引号键:词边界用环视(键名可能以转义序列开头,\b 不适用)
_KEY_U = rf"(?<![A-Za-z0-9_])(?:{_KEY_ALT})(?![A-Za-z0-9_])"

# 值:带引号段(单/双引号内部允许 \\' \\" 转义;支持 bash 引号拼接 'a'\\''b')
# 或无引号(到 , ; } 、与行尾,允许值内空格)
# 转义引号定界(二次序列化层:{\"password\":\"a,b\"}):边界是 \"(1 个反斜杠+引号);
# 值内容里的编码引号(\\\",3 个反斜杠+引号)与编码反斜杠(\\)不是边界,
# 值内的 , ; } 不得终止脱敏(避免带分隔符密码尾部泄露),兄弟字段不误伤
_SQ = r"'(?:[^'\\]|\\.)*'"
_DQ = r"\"(?:[^\"\\]|\\.)*\""
_SQ_ESC = r"\\'(?:\\\\\\'|\\\\|[^'\\])*\\'"
_DQ_ESC = r"\\\"(?:\\\\\\\"|\\\\|[^\"\\])*\\\""
_QUOTED_SEG = rf"(?:{_DQ_ESC}|{_SQ_ESC}|{_SQ}|{_DQ}|\\')"
_VAL = rf"(?:{_QUOTED_SEG}+|[^,;}}\n\r、]+)"


def _redact_value(match: re.Match) -> str:
    """保留键与分隔符;值整体打码(带引号值保留引号形态,含转义引号定界)。"""
    prefix = match.group("prefix")
    val = match.group("val")
    if val[:1] in ('"', "'"):
        return f"{prefix}{val[0]}{REDACTED}{val[0]}"
    if val[:2] in ('\\"', "\\'"):
        return f"{prefix}{val[:2]}{REDACTED}{val[:2]}"
    return f"{prefix}{REDACTED}"


# 顺序敏感:先长块(私钥),再行内键值,再独立 token 形态
_PATTERNS: list[tuple[re.Pattern, Any]] = [
    # PEM 私钥块(含 BEGIN/END 标记行)
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
        REDACTED,
    ),
    # PEM 只有 BEGIN、缺 END(被截断/输入不完整):保守打码到结尾,避免半截材料外泄
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*"),
        REDACTED,
    ),
    # JSON/带引号键:{"password":"x"}、{\"password\":\"x\"}、" password ":"x"
    (
        re.compile(rf"(?i)(?P<prefix>{_KEY_Q}\s*[:=]\s*)(?P<val>{_VAL})"),
        _redact_value,
    ),
    # 无引号键:password=x / Authorization: Bearer x y
    (
        re.compile(rf"(?i)(?P<prefix>{_KEY_U}\s*[:=]\s*)(?P<val>{_VAL})"),
        _redact_value,
    ),
    # URL 内嵌凭据 scheme://user:pass@host
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@"),
        r"\1\2:" + REDACTED + "@",
    ),
    # JWT 三段式
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), REDACTED),
    # AWS Access Key ID
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), REDACTED),
    # 长十六进制密钥(32+ 位连续 hex,排除太常见的短哈希误伤由长度控制)
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), REDACTED),
]


def sanitize_text(text: str) -> str:
    """对文本做敏感信息打码;空串原样返回。"""
    if not text:
        return text
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def sanitize_mapping(data):
    """递归处理 dict/list 中的字符串值。"""
    if isinstance(data, str):
        return sanitize_text(data)
    if isinstance(data, dict):
        return {k: sanitize_mapping(v) for k, v in data.items()}
    if isinstance(data, list):
        return [sanitize_mapping(v) for v in data]
    return data
