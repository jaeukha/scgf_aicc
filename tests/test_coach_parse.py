"""model_server의 Coach 출력 형식 보정: 형식만 고치고 내용 검증은 그대로인지."""
import pytest

import model_server as ms
from aicc.providers import ground_terminal_guidance


def test_terminal_guidance_uses_routing_facts_not_model_claims():
    route = {"paths": [{"topic": "만기·연장", "action": "업무시스템에서 만기일을 확인하세요.",
                         "steps": [{"title": "만기 시점", "choice": "만기 전", "certain": True},
                                   {"title": "영업 상황", "choice": "정상", "certain": True}]}]}
    out = ground_terminal_guidance({"staff_note": "고객이 연체를 문의했습니다.", "customer_question": "언제인가요?",
                                    "action_items": ["연체 확인"]}, route)
    assert out == {"staff_note": "만기·연장 상담입니다. 확인된 내용: 만기 시점: 만기 전, 영업 상황: 정상. 업무시스템 확인 후 안내가 필요합니다.",
                   "customer_question": "", "action_items": ["업무시스템에서 만기일을 확인하세요."]}


def test_repairs_format_only():
    assert ms.tolerant_parse('{"staff_note": "고객이 "분할상환"을 원합니다.", "customer_question": "", "action_items": []}')["staff_note"] == '고객이 "분할상환"을 원합니다.'
    out = ms.tolerant_parse('설명\n```json\n{"staff_note":"a","customer_question":"b?","action_items":["x","y","z"],"extra":1}\n```')
    assert out == {"staff_note": "a", "customer_question": "b?", "action_items": ["x", "y"]}
    with pytest.raises(ValueError):  # 빈 직원 설명은 여전히 거절
        ms.tolerant_parse('{"staff_note":"","customer_question":"x","action_items":[]}')
    with pytest.raises(ValueError):  # JSON이 아예 없으면 거절
        ms.tolerant_parse("죄송합니다. 답변할 수 없습니다.")


def test_merges_split_objects():
    out = ms.tolerant_parse('{"staff_note": "연체 상담"}\n{"customer_question": "언제까지 필요하신가요?"}\n{"action_items": ["담당자 연결"]}')
    assert out == {"staff_note": "연체 상담", "customer_question": "언제까지 필요하신가요?", "action_items": ["담당자 연결"]}
