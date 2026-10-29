"""合成适配器:仅验证协议、引用观察和指标机制,不代表真实模型能力。

- 合成 embedding:概念词袋 one-hot;只用于构建/查询同一合成索引,不能证明语义效果;
- 合成聊天:根据提示词中的 `[EVAL:<sample_id>]` 返回预定义结构/故障行为;
  原始输出只保留在进程内供评测观察,不写入业务 API/报告;
- 所有调用由评测进程计数,默认零外部网络、零真实密钥。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from app.ai.embeddings import EmbeddingResult
from app.ai.llm import LLMError, LLMRequest, LLMResult
from app.eval.datasets import Dataset

_MARKER_RE = re.compile(r"\[EVAL:([A-Za-z0-9_-]+)\]")
_REF_RE = re.compile(
    r"\[(case|sop):(\d+)\][^\n]*\(source_type=(case|sop), source_id=(\d+), "
    r"version=(null|\d+)\)"
)
_EVIDENCE_RE = re.compile(r"^\[([A-Za-z0-9:_-]+)\] \(", re.MULTILINE)

CONCEPTS: list[tuple[str, set[str]]] = [
    ("oom", {"oomkilled", "oom", "内存", "memory", "内存溢出"}),
    ("probe", {"探针", "probe", "liveness", "readiness", "404"}),
    ("port", {"端口", "targetport", "err_connection_refused", "connection refused", "连接被拒", "连不上"}),
    ("dns", {"dns", "域名", "解析", "lookup", "no such host", "coredns"}),
    ("cert", {"证书", "certificate", "tls", "expired", "过期", "握手"}),
    ("redis", {"redis", "连接池", "connection pool", "maxclients", "缓存"}),
    ("lock", {"锁", "lock", "事务", "等待"}),
    ("timeout", {"超时", "timeout", "延迟", "响应慢", "接口慢", "请求超时"}),
    ("disk", {"磁盘", "no space left", "df -h", "磁盘空间"}),
    ("version", {"v1-legacy-only", "v2-current-only", "版本链"}),
    ("private", {"private-pending-only", "private-returned-only", "draft-only", "submitted-only", "deprecated-only", "待审", "退回", "草稿", "废弃"}),
    ("injection", {"忽略", "指令", "ignore previous", "rm -rf", "sql drop", "注入"}),
]
_VOCAB = [name for name, _ in CONCEPTS] + ["_unknown"]
_DIMS = len(_VOCAB)


def synthetic_vector(text: str) -> list[float]:
    lowered = (text or "").lower()
    vec = [0.0] * _DIMS
    for name, terms in CONCEPTS:
        if any(term in lowered for term in terms):
            vec[_VOCAB.index(name)] = 1.0
    if sum(vec) == 0.0:
        vec[-1] = 1.0
    return vec


class SyntheticEmbeddingAdapter:
    """评测专用 embedding 适配器(概念词袋);零外部调用。"""

    mode = "synthetic-eval-embedding"

    def __init__(self):
        self.calls: list[dict] = []

    def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls.append({"count": len(texts), "chars": [len(t) for t in texts]})
        return EmbeddingResult(
            vectors=[synthetic_vector(t) for t in texts],
            usage=None,
            model=self.mode,
            dims=_DIMS,
        )

    def describe(self) -> dict:
        return {"mode": self.mode, "dims": _DIMS, "calls": len(self.calls)}


@dataclass
class SyntheticCall:
    sample_id: str
    call_index: int
    behavior: str
    prompt_refs: list[dict] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    usage: dict | None = None
    raw_text: str = ""
    exception_code: str = ""

    def raw_metadata(self) -> dict:
        return {
            "sample_id": self.sample_id,
            "call_index": self.call_index,
            "behavior": self.behavior,
            "prompt_refs": list(self.prompt_refs),
            "evidence_ids": list(self.evidence_ids),
            "usage": self.usage,
            "raw_sha256": hashlib.sha256(self.raw_text.encode("utf-8")).hexdigest()
            if self.raw_text
            else "",
            "raw_length": len(self.raw_text),
            "exception_code": self.exception_code,
        }


def parse_prompt_refs(prompt: str) -> list[dict]:
    refs = []
    seen = set()
    for m in _REF_RE.finditer(prompt or ""):
        ref_id = f"{m.group(1)}:{m.group(2)}"
        if ref_id in seen:
            continue
        seen.add(ref_id)
        version = None if m.group(5) == "null" else int(m.group(5))
        refs.append(
            {
                "ref_id": ref_id,
                "source_type": m.group(3),
                "source_id": int(m.group(4)),
                "version": version,
            }
        )
    return refs


def parse_evidence_ids(prompt: str) -> list[str]:
    return list(dict.fromkeys(_EVIDENCE_RE.findall(prompt or "")))


class SyntheticChatAdapter:
    """按样本 ID 返回合法/故障输出的合成聊天适配器。"""

    def __init__(self, dataset: Dataset, behaviours: dict[str, str] | None = None):
        self.dataset = dataset
        self.behaviours = dict(behaviours or dataset.behaviours)
        self.calls: list[SyntheticCall] = []
        self.before_return = None  # callable(sample_id, prompt_refs) for source invalidation

    def _next_index(self, sample_id: str) -> int:
        return sum(1 for c in self.calls if c.sample_id == sample_id)

    def _valid_payload(self, sample_id: str, prompt: str, behavior: str) -> dict:
        label = self.dataset.labels.get(sample_id)
        refs = parse_prompt_refs(prompt)
        evidence = parse_evidence_ids(prompt)
        chosen_refs = refs[:2]
        if behavior == "invalid_reference":
            chosen_refs.append(
                {
                    "ref_id": "case:999999",
                    "source_type": "case",
                    "source_id": 999999,
                    "version": None,
                }
            )
        category = None
        abstain = bool(label and label.abstain)
        if not abstain:
            category = (label.expected_category if label and label.expected_category else None) or (
                label.allowed_categories[0] if label and label.allowed_categories else "待分类"
            )
        facts = []
        for eid in evidence[:2]:
            facts.append({"evidence_ids": [eid], "fact": f"合成评测:输入证据 {eid} 提供信息"})
        hypotheses = []
        if not abstain:
            hypotheses.append(
                {
                    "title": "合成评测候选原因",
                    "rationale": "合成评测:依据输入证据整理,不代表真实模型判断",
                    "evidence_ids": evidence[:1],
                    "reference_ids": [r["ref_id"] for r in chosen_refs[:1]],
                    "missing_information": ["合成评测:需人工复核"],
                    "verification": "合成评测:人工核验",
                    "priority": "证据不足",
                }
            )
        payload = {
            "summary": f"合成评测输出:{sample_id}",
            "observed_facts": facts,
            "suggested_category": category,
            "suggested_tags": ["合成评测"],
            "hypotheses": hypotheses,
            "missing_information": ["合成评测:参考标签为 provisional,需人工复核"],
            "next_checks": [],
            "references": [
                {
                    "ref_id": r["ref_id"],
                    "source_type": r["source_type"],
                    "source_id": r["source_id"],
                    "version": r["version"],
                    "relevance": "合成评测:提示词下发片段",
                }
                for r in chosen_refs
            ],
            "insufficient_evidence": abstain,
        }
        return payload

    def chat(self, request: LLMRequest) -> LLMResult:
        # 格式修复请求会在原始用户提示后追加 assistant/feedback;样本标记与
        # 下发片段必须取第一条 user 消息,不能用最后一条反馈消息(否则丢失 marker)。
        user_messages = [m.content for m in request.messages if m.role == "user"]
        prompt = user_messages[0] if user_messages else (
            request.messages[-1].content if request.messages else ""
        )
        marker = _MARKER_RE.search(prompt)
        sample_id = marker.group(1) if marker else "unknown"
        behavior = self.behaviours.get(sample_id, "valid")
        call_index = self._next_index(sample_id)
        prompt_refs = parse_prompt_refs(prompt)
        evidence_ids = parse_evidence_ids(prompt)
        usage = {"prompt_tokens": 12, "completion_tokens": 20, "total_tokens": 32}
        if behavior == "partial_usage":
            usage = {"prompt_tokens": 7}

        call = SyntheticCall(
            sample_id=sample_id,
            call_index=call_index,
            behavior=behavior,
            prompt_refs=prompt_refs,
            evidence_ids=evidence_ids,
            usage=usage,
        )
        self.calls.append(call)

        if behavior in ("timeout", "rate_limited"):
            call.usage = None
            call.exception_code = behavior
            raise LLMError(
                behavior,
                "合成评测:模拟模型超时" if behavior == "timeout" else "合成评测:模拟限流",
                retryable=True,
            )

        if behavior == "bad_json":
            text = "这不是 JSON;合成评测非法输出"
        elif behavior == "schema_bad":
            # summary 必须是字符串;整数会触发真实 Schema 校验失败
            text = json.dumps({"summary": 123}, ensure_ascii=False)
        elif behavior == "schema_repair_then_valid":
            if call_index == 0:
                text = json.dumps({"summary": 123}, ensure_ascii=False)
            else:
                text = json.dumps(
                    self._valid_payload(sample_id, prompt, "valid"), ensure_ascii=False
                )
        else:
            payload = self._valid_payload(sample_id, prompt, behavior)
            if behavior == "source_invalidation" and self.before_return is not None:
                self.before_return(sample_id, prompt_refs)
            text = json.dumps(payload, ensure_ascii=False)

        call.raw_text = text
        return LLMResult(text=text, usage=usage, model="synthetic-eval-chat", latency_ms=0)

    def describe(self) -> dict:
        return {
            "mode": "synthetic-eval-chat",
            "calls": len(self.calls),
            "samples": len({c.sample_id for c in self.calls}),
        }


def call_metadata(calls: list[SyntheticCall]) -> list[dict]:
    """供报告使用的安全元数据:不含原始模型正文/提示词。"""
    return [c.raw_metadata() for c in calls]
