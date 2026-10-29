"""从流式 JSON 输出中增量提取 answer 字段,供对话边生成边渲染 Markdown。

模型仍按 ChatAnswer 输出完整 JSON;这里只做增量解码:
- 找到 "answer" 键后,按 JSON 字符串转义规则解码并逐段返回文本;
- 支持转义序列被拆分到不同 chunk(结尾反斜杠、\\uXXXX 未收全时等待下一段);
- 键出现前只保留末尾窗口,避免缓冲无限增长;
- 找不到该字段(或输出非法)时返回空,回答在最终消息中一次性出现,不影响正确性。
"""

from __future__ import annotations

import re

_ANSWER_KEY = re.compile(r'"answer"\s*:\s*"')
_SIMPLE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
# 键名/冒号/引号可能被拆到两个 chunk;保留足够尾部字符继续匹配
_KEEP_TAIL = 32


class AnswerStreamExtractor:
    def __init__(self) -> None:
        self._buf = ""
        self._searching = True
        self._finished = False

    @property
    def finished(self) -> bool:
        return self._finished

    def feed(self, chunk: str) -> str:
        """追加一段模型原始输出,返回本次新解码出的 answer 文本。"""
        if self._finished or not chunk:
            return ""
        self._buf += chunk

        if self._searching:
            match = _ANSWER_KEY.search(self._buf)
            if match is None:
                if len(self._buf) > _KEEP_TAIL:
                    self._buf = self._buf[-_KEEP_TAIL:]
                return ""
            self._buf = self._buf[match.end() :]
            self._searching = False

        out: list[str] = []
        index = 0
        length = len(self._buf)
        while index < length:
            char = self._buf[index]
            if char == '"':
                self._finished = True
                self._buf = ""
                break
            if char != "\\":
                out.append(char)
                index += 1
                continue
            if index + 1 >= length:
                break  # 转义符在末尾,等待下一段
            escape = self._buf[index + 1]
            simple = _SIMPLE_ESCAPES.get(escape)
            if simple is not None:
                out.append(simple)
                index += 2
                continue
            if escape != "u":
                # 非法转义:原样保留,避免吞掉内容
                out.append("\\" + escape)
                index += 2
                continue
            if index + 6 > length:
                break  # \\uXXXX 未收全
            hex4 = self._buf[index + 2 : index + 6]
            try:
                code = int(hex4, 16)
            except ValueError:
                out.append(self._buf[index : index + 2])
                index += 2
                continue
            if 0xD800 <= code <= 0xDBFF:
                # 高代理项:需要紧跟低代理项;未收全时等待下一段
                if index + 12 > length:
                    break
                if self._buf[index + 6 : index + 8] == "\\u":
                    try:
                        low = int(self._buf[index + 8 : index + 12], 16)
                    except ValueError:
                        low = -1
                    if 0xDC00 <= low <= 0xDFFF:
                        out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                        index += 12
                        continue
                out.append("\ufffd")
                index += 6
                continue
            out.append(chr(code))
            index += 6

        self._buf = self._buf[index:]
        return "".join(out)