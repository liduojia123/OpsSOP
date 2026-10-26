"""极简 HTTP 客户端(标准库),用于演练的真实登录/业务/权限检查。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .common import OpsError


class HttpResponse:
    def __init__(self, status: int, body: Any, text: str):
        self.status = status
        self.body = body
        self.text = text


class HttpClient:
    def __init__(self, base_url: str, *, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = ""

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        expected: tuple[int, ...] = (200, 201),
        allow_any: bool = False,
    ) -> HttpResponse:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            status = exc.code
        except urllib.error.URLError as exc:
            raise OpsError(f"HTTP 请求失败: {method} {path}: {exc.reason}", stage="http") from exc
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = None
        if not allow_any and status not in expected:
            detail = body.get("detail") if isinstance(body, dict) else raw[:300]
            raise OpsError(
                f"HTTP {method} {path} 返回 {status}: {str(detail)[:300]}",
                stage="http",
            )
        return HttpResponse(status, body, raw)

    def login(self, username: str, password: str) -> dict:
        resp = self._request(
            "POST",
            "/api/auth/login",
            json_body={"username": username, "password": password},
            expected=(200,),
        )
        if not isinstance(resp.body, dict) or not resp.body.get("token"):
            raise OpsError("登录响应缺少 token", stage="http")
        self.token = resp.body["token"]
        return resp.body

    def get(self, path: str, *, expected: tuple[int, ...] = (200,)) -> HttpResponse:
        return self._request("GET", path, expected=expected)

    def post(
        self,
        path: str,
        body: dict | None = None,
        *,
        expected: tuple[int, ...] = (200, 201),
    ) -> HttpResponse:
        return self._request("POST", path, json_body=body, expected=expected)

    def patch(self, path: str, body: dict, *, expected: tuple[int, ...] = (200,)) -> HttpResponse:
        return self._request("PATCH", path, json_body=body, expected=expected)

    def raw(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
    ) -> HttpResponse:
        return self._request(method, path, json_body=json_body, allow_any=True)
