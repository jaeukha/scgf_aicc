"""Twenty 워크스페이스에 AICC 상담 사용자 정의 객체를 만든다(이미 있으면 건너뜀).

사용: TWENTY_API_KEY=... python twenty/create_schema.py   (TWENTY_BASE_URL 기본 http://127.0.0.1:3000)
"""
import os

import httpx

BASE = os.environ.get("TWENTY_BASE_URL", "http://127.0.0.1:3000")
HEADERS = {"Authorization": "Bearer " + os.environ["TWENTY_API_KEY"]}
FIELDS = [("aiccSessionId", "AICC 세션 ID", "TEXT"), ("topic", "상담 주제", "TEXT"),
          ("reviewStatus", "검토 상태", "TEXT"), ("summary", "요약", "TEXT"), ("startedAt", "시작 시각", "DATE_TIME")]


def gql(query, variables=None):
    r = httpx.post(BASE + "/metadata", json={"query": query, "variables": variables or {}}, headers=HEADERS, timeout=30)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(body["errors"][0]["message"])
    return body["data"]


objects = {e["node"]["nameSingular"]: e["node"]["id"]
           for e in gql("{objects(paging:{first:200}){edges{node{id nameSingular}}}}")["objects"]["edges"]}
if "aiccConsultation" in objects:
    print("aiccConsultation 이미 존재 — 건너뜀")
    raise SystemExit
oid = gql("mutation($i:CreateOneObjectInput!){createOneObject(input:$i){id}}", {"i": {"object": {
    "nameSingular": "aiccConsultation", "namePlural": "aiccConsultations", "labelSingular": "AICC 상담",
    "labelPlural": "AICC 상담 목록", "icon": "IconHeadset"}}})["createOneObject"]["id"]
field = "mutation($i:CreateOneFieldMetadataInput!){createOneField(input:$i){id}}"
for name, label, kind in FIELDS:
    gql(field, {"i": {"field": {"objectMetadataId": oid, "name": name, "label": label, "type": kind}}})
gql(field, {"i": {"field": {"objectMetadataId": oid, "name": "company", "label": "고객(회사)", "type": "RELATION",
    "relationCreationPayload": {"targetObjectMetadataId": objects["company"], "targetFieldLabel": "AICC 상담",
                                "targetFieldIcon": "IconHeadset", "type": "MANY_TO_ONE"}}}})
print("aiccConsultation 객체·필드 생성 완료")
