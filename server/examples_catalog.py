# -*- coding: utf-8 -*-
"""내장 예제 목록. 모든 치수·요건은 가정값이며 현황 도면은 규칙으로 생성한 합성 도면이다."""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DR = os.path.join(ROOT, "examples", "drawings")
EX = os.path.join(ROOT, "examples")

_CATALOG = [
    dict(name="smr_aux", kind="drawing", group="원전",
         title="원전 — SMR 보조건물 3개 층 (DXF, 44기기)",
         path=os.path.join(DR, "smr_aux_3f.dxf"),
         requirements=os.path.join(DR, "smr_aux_3f_requirements.json"),
         import_options={},
         project_options={"preset": "space_first"},
         why="원전 고유 제약 7종(화재분리 App.R, 해치 화재구역 병합, 다층 관통, 터빈 미사일, 중요구역, 하중경로, 공지) + 중력구동 표고 + 크레인."),
    dict(name="chem_plant", kind="drawing", group="비교군",
         title="비교군 — 석유화학 공정 플랜트 (벡터 PDF 1:500)",
         path=os.path.join(DR, "chem_plant.pdf"),
         requirements=os.path.join(DR, "chem_plant_requirements.json"),
         import_options={"scale_ratio": 500},
         project_options={"preset": "space_first"},
         why="가열로·압축기·저장탱크·제어실 간 화재·폭발 이격, 공정 배관 연결, 고정 파이프랙 — 원전 화재분리와 같은 구조."),
    dict(name="fpso_topside", kind="drawing", group="비교군",
         title="비교군 — FPSO 상부 모듈 2개 데크 (SVG)",
         path=os.path.join(DR, "fpso_topside.svg"),
         requirements=os.path.join(DR, "fpso_topside_requirements.json"),
         import_options={},
         project_options={"preset": "space_first"},
         why="갑판 면적 제약이 가장 직접적, 무게중심 편심(내진 비틀림과 같은 항), 위험구역↔거주구역 분리, 고정 터렛·크레인."),
    dict(name="hospital_or", kind="drawing", group="비교군",
         title="비교군 — 병원 수술부 (PNG 래스터 평면도)",
         path=os.path.join(DR, "hospital_or.png"),
         requirements=os.path.join(DR, "hospital_or_requirements.json"),
         import_options={"px_per_m": 10},
         project_options={"preset": "space_first", "default_clearance": 0.0, "split_corridors": False},
         why="청결/오염 구역(방사선구역 분리와 같은 구조), 수술실-멸균코어 인접 동선. 실(室) 단위 평면계획."),
    dict(name="smr_aux_ir", kind="ir", group="원전",
         title="원전 — SMR 보조건물 IR (도면 없이, 자연어 경로)",
         path=os.path.join(EX, "spec_smr_aux.json"),
         why="도면이 없을 때의 경로. 현황 비교 대신 나이브 기준안과만 비교된다."),
    dict(name="minimal_ir", kind="ir", group="학습",
         title="학습용 — 5블록 IR",
         path=os.path.join(EX, "spec_minimal.json"),
         why="가장 작은 사례."),
]


def catalog():
    return [public(e) for e in _CATALOG if os.path.exists(e["path"])]


def public(e):
    return dict(name=e["name"], title=e["title"], group=e["group"], kind=e["kind"],
                file=os.path.basename(e["path"]), has_requirements=bool(e.get("requirements")),
                why=e["why"], note="가정값·합성 도면 — 실제 설계값 아님. 개선율을 성과로 쓰지 말 것.")


def get(name):
    for e in _CATALOG:
        if e["name"] == name and os.path.exists(e["path"]):
            return e
    return None
