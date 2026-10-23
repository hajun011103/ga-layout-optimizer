# -*- coding: utf-8 -*-
"""
irhash.py — IR / 결과의 정준(canonical) 해시.

Gate 1에서 승인된 IR은 여기서 계산한 SHA-256으로 동결된다.
이후 전 과정은 이 해시로부터 LLM 호출 0회로 재현 가능해야 한다.
(보고서 05장 불변식 2)

정준화 규칙
  - dict 키 정렬, 공백 제거, ensure_ascii=False, 부동소수 반올림 없음
  - 해시 계산 대상에서 '_' 로 시작하는 키(주석/메모)는 제외한다
"""
from __future__ import annotations
import hashlib
import json
from typing import Any


def strip_comments(obj: Any) -> Any:
    """'_' 로 시작하는 키(사람용 주석)를 재귀적으로 제거."""
    if isinstance(obj, dict):
        return {k: strip_comments(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [strip_comments(v) for v in obj]
    return obj


def canonical(obj: Any) -> str:
    return json.dumps(strip_comments(obj), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def sha256_obj(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def sha256_file(path: str) -> str:
    with open(path, encoding="utf-8") as fp:
        return sha256_obj(json.load(fp))


def short(h: str, n: int = 12) -> str:
    return h[:n]
