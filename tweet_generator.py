"""
마푸잉 2-에이전트 트윗 생성기
- 에이전트 A (HookGenerator): Hook(첫 문장) 생성
- 에이전트 B (BodyGenerator): Hook 이어받아 Body 생성
- TopicRecommender: 10개 토픽 우선순위 추천
- 로컬 전용: 외부 API 호출 없음, 템플릿+데이터 기반 규칙 생성
"""

import random
import sys
import io
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import unicodedata

from reverse_engineer_tweets import (
    extract_hook, classify_hook, classify_body,
    format_number, truncate_text,
    HOOK_LABELS_KR, BODY_LABELS_KR, URL_RE,
)
from customize_mapuing import (
    load_mapuing_tweets, classify_topic,
    analyze_topics, analyze_mapuing_unique_patterns,
    TOPIC_PATTERNS, MAPUING_PATTERNS,
)

# ─────────────────────────────────────────────
# 상수: 주제별 세부 토픽
# ─────────────────────────────────────────────

SUB_TOPICS = {
    "기술/유틸 팁": [
        "아이폰 배터리 오래 쓰는 설정",
        "갤럭시 숨겨진 기능",
        "스마트폰 보안 설정",
        "스팸 전화 완벽 차단법",
        "아이폰 카메라 꿀팁",
        "갤럭시 배터리 절약 설정",
        "Wi-Fi 보안 설정",
        "스마트폰 저장공간 확보법",
        "아이폰 사생활 보호 설정",
        "블루투스 이어폰 숨은 기능",
    ],
    "인간관계/심리": [
        "진짜 친구와 가짜 친구의 차이",
        "오래가는 관계와 금방 끝나는 관계",
        "솔직함과 무례함의 차이",
        "매너와 위선의 차이",
        "자신감과 허세의 차이",
        "호감 가는 대화법과 불쾌한 대화법",
        "리더십과 권위주의의 차이",
        "가스라이팅 알아채는 신호",
        "인간관계에서 손절해야 할 사람 특징",
        "첫인상에서 호감을 주는 사람의 특징",
    ],
    "정부혜택/제도": [
        "청년 정부지원금",
        "실업급여 신청 방법",
        "주택청약 필수 정보",
        "연말정산 공제 항목",
        "국민내일배움카드 활용법",
        "건강보험 환급 방법",
        "정부 복지 보조금",
        "세금 절약 방법",
        "전세사기 예방법",
        "국민연금 수령 팁",
    ],
    "금융/투자": [
        "초보 투자자 필수 지식",
        "ETF 투자 입문 가이드",
        "월급 관리 재테크 방법",
        "배당주 추천 기준",
        "부동산 투자 체크리스트",
        "저축 습관 만드는 방법",
        "대출 이자 줄이는 법",
        "경제 뉴스 읽는 법",
        "코인 투자 주의사항",
        "자산 포트폴리오 구성법",
    ],
}

# 주제 → 최적 공식 매핑
TOPIC_FORMULA_MAP = {
    "기술/유틸 팁": "🚨경고/긴급+실용정보",
    "인간관계/심리": "A와 B 대비형",
    "정부혜택/제도": "손실회피+정부혜택",
    "금융/투자": "N가지 실용 리스트",
}

# 10개 슬롯 배분: 인간관계 3, 기술 3, 정부혜택 2, 금융 2
SLOT_DISTRIBUTION = [
    "인간관계/심리", "기술/유틸 팁", "정부혜택/제도",
    "인간관계/심리", "기술/유틸 팁", "금융/투자",
    "인간관계/심리", "기술/유틸 팁", "정부혜택/제도",
    "금융/투자",
]

# ─────────────────────────────────────────────
# 슬롯 필러
# ─────────────────────────────────────────────

SLOT_FILLERS = {
    "device": ["아이폰", "갤럭시", "스마트폰"],
    "n": ["3", "4", "5", "6", "7", "10"],
    "n_small": ["3", "4", "5"],
    "a_b_pairs": [
        ("오래가는 관계", "금방 끝나는 관계"),
        ("매너", "위선"),
        ("솔직함", "무례함"),
        ("자신감", "허세"),
        ("리더십", "권위주의"),
        ("호감 가는 대화법", "불쾌한 대화법"),
        ("진짜 친구", "가짜 친구"),
        ("배려", "간섭"),
        ("칭찬", "아부"),
        ("조언", "잔소리"),
    ],
    "age": ["30", "40", "50", "60"],
    "benefit_target": ["청년", "직장인", "자영업자", "신혼부부", "은퇴자"],
    "emoji_warning": ["🚨", "⚠️", "❗"],
    "emoji_list": ["📌", "💡", "🔧", "📸", "✅"],
}

# ─────────────────────────────────────────────
# Body 콘텐츠 풀
# ─────────────────────────────────────────────

BODY_CONTENT = {
    "기술/유틸 팁": {
        "warning_items": [
            ("위치 서비스 정리", "설정 → 개인정보 보호 → 위치 서비스", "배터리 최대 20% 절약"),
            ("백그라운드 앱 새로고침 끄기", "설정 → 일반 → 백그라운드 앱 새로고침", "데이터·배터리 동시 절약"),
            ("알림 정리", "설정 → 알림 → 불필요한 앱 OFF", "집중력 향상 + 배터리 절약"),
            ("자동 밝기 설정", "설정 → 디스플레이 → 자동 밝기 ON", "눈 보호 + 배터리 절약"),
            ("사진 최적화", "설정 → 카메라 → 고효율 포맷", "저장공간 50% 절약"),
            ("메일 자동 로드 끄기", "설정 → 메일 → 자동 가져오기 OFF", "백그라운드 데이터 절약"),
            ("광고 추적 제한", "설정 → 개인정보 보호 → 추적 → OFF", "개인정보 보호 강화"),
            ("시스템 햅틱 끄기", "설정 → 사운드 및 햅틱 → 시스템 햅틱 OFF", "배터리 소모 감소"),
            ("자동 업데이트 Wi-Fi만", "설정 → 앱스토어 → 셀룰러 데이터 OFF", "데이터 요금 절약"),
            ("스팸 문자 필터링", "설정 → 메시지 → 알 수 없는 발신자 필터링", "스팸 완벽 차단"),
            ("핫스팟 자동 연결 해제", "설정 → 개인용 핫스팟 → 자동 허용 OFF", "보안 강화"),
            ("잠금화면 알림 미리보기 끄기", "설정 → 알림 → 미리보기 → 잠금 해제 시", "사생활 보호"),
        ],
        "list_items": [
            ("화면 녹화", "전원+음량 동시 누르기 → 편집 → 공유"),
            ("숨긴 사진 잠금", "사진 → 숨김 앨범 → Face ID 잠금 설정"),
            ("배터리 상태 확인", "설정 → 배터리 → 배터리 성능 상태 확인"),
            ("단축어 자동화", "단축어 앱 → 자동화 → 시간/위치 기반 설정"),
            ("라이브 텍스트", "카메라로 텍스트 비추기 → 바로 복사/번역"),
            ("사파리 탭 그룹", "사파리 → 탭 버튼 길게 → 탭 그룹 생성"),
            ("집중 모드 설정", "설정 → 집중 모드 → 업무/수면/개인 커스텀"),
            ("배경화면 자동 변경", "설정 → 배경화면 → 사진 셔플 설정"),
            ("키보드 단축키", "텍스트 대치 등록 → 자주 쓰는 문구 저장"),
            ("위젯 스택", "홈화면 편집 → 위젯 겹치기 → 스마트 회전 ON"),
        ],
    },
    "인간관계/심리": {
        "positive_traits": [
            "힘들 때 먼저 연락이 옴",
            "성공했을 때 진심으로 축하해줌",
            "뒤에서도 좋게 말함",
            "약속을 잘 지킴",
            "솔직한 피드백을 해줌",
            "경계를 존중해줌",
            "실수했을 때 함께 해결책을 찾아줌",
            "대화할 때 끝까지 들어줌",
            "작은 변화도 알아챔",
            "어려울 때 시간을 내줌",
        ],
        "negative_traits": [
            "잘 될 때만 연락이 옴",
            "뒤에서 험담함",
            "약속을 자주 어김",
            "자기 얘기만 함",
            "은근히 비교하며 깎아내림",
            "도움 준 걸 계속 언급함",
            "성공하면 시기함",
            "비밀을 지키지 않음",
            "필요할 때만 찾음",
            "감정을 무시함",
        ],
        "contrast_labels": [
            ("오래가는 관계", "금방 끝나는 관계"),
            ("진짜 친구", "가짜 친구"),
            ("매너 있는 사람", "위선적인 사람"),
            ("자신감 있는 사람", "허세 부리는 사람"),
            ("솔직한 사람", "무례한 사람"),
            ("좋은 리더", "권위적인 상사"),
            ("배려하는 사람", "간섭하는 사람"),
            ("호감 가는 사람", "불쾌한 사람"),
        ],
    },
    "정부혜택/제도": {
        "age_benefits": {
            "20대": [
                ("청년내일저축계좌", "매월 10만원 저축 → 3년 후 최대 1,440만원"),
                ("청년 월세 지원", "월 최대 20만원 × 12개월"),
                ("국민내일배움카드", "최대 500만원 훈련비 지원"),
                ("청년 교통비 지원", "연 최대 12만원 교통비 환급"),
            ],
            "30대": [
                ("신혼부부 전세자금 대출", "최대 3억원, 연 1.5% 금리"),
                ("출산 크레딧", "국민연금 가입기간 추가 인정"),
                ("육아휴직 급여", "통상임금의 80%, 최대 150만원/월"),
                ("주택청약 소득공제", "납입액의 40%, 연 최대 96만원"),
            ],
            "40대": [
                ("근로장려금", "가구당 최대 330만원"),
                ("자녀장려금", "자녀 1인당 최대 80만원"),
                ("국민연금 추납", "미납 기간 추후 납부 가능"),
                ("건강보험 환급", "본인부담 상한액 초과분 환급"),
            ],
            "50대": [
                ("국민취업지원제도", "월 50만원 × 6개월 구직촉진수당"),
                ("퇴직연금 세액공제", "연 최대 900만원 납입분 공제"),
                ("장기요양보험", "등급 인정 시 재가·시설 서비스"),
                ("기초연금", "월 최대 32만원 (만 65세 이상)"),
            ],
        },
    },
    "금융/투자": {
        "list_items": [
            ("비상금 통장", "월급의 3~6개월분 → CMA 또는 파킹통장에 분리"),
            ("자동이체 설정", "월급일 다음날 → 저축·투자 자동이체 설정"),
            ("소비 패턴 분석", "가계부 앱으로 3개월 추적 → 불필요 지출 찾기"),
            ("연금저축 가입", "연 최대 600만원 세액공제 → 노후 준비"),
            ("ETF 적립식 투자", "매월 정해진 금액 → S&P500 또는 코스피200"),
            ("신용점수 관리", "올크레딧/나이스 앱 → 분기별 확인"),
            ("보험 리모델링", "불필요한 보험 정리 → 보장 분석 서비스 활용"),
            ("배당주 포트폴리오", "분기 배당 → 현금흐름 만들기"),
            ("부동산 갭투자 주의", "전세가율 70% 이상 → 역전세 리스크 확인"),
            ("환율 우대 계좌", "해외주식 투자 시 환전 수수료 90% 우대"),
        ],
    },
}

# ─────────────────────────────────────────────
# 마푸잉 고성과 Hook 원본 (변형용)
# ─────────────────────────────────────────────

MAPUING_HIGH_HOOKS = {
    "기술/유틸 팁": [
        "🚨아이폰 유저 지금 당장 해야 할 설정",
        "🚨갤럭시 유저 필수 설정 5가지🚨",
        "⚠️스마트폰 이 설정 안 하면 손해",
        "🚨절대 안 하면 후회하는 폰 설정",
    ],
    "인간관계/심리": [
        "오래가는 관계와 금방 끝나는 관계",
        "솔직함과 무례함의 차이",
        "매너와 위선의 차이",
        "자신감과 허세의 차이",
    ],
    "정부혜택/제도": [
        "🧓50살 전에 신청 안하면 못 받는 정부 지원 혜택",
        "🚨대부분 모르는 청년 지원금 5가지",
        "지금 안 하면 못 받는 정부 혜택 총정리",
    ],
    "금융/투자": [
        "알아두면 언젠간 써먹는 금융 상식 7가지",
        "직장인 월급 관리 꿀팁 정리",
        "초보 투자자가 반드시 알아야 할 것들",
    ],
}


# ─────────────────────────────────────────────
# TopicRecommender — 토픽 점수 & 우선순위
# ─────────────────────────────────────────────

class TopicRecommender:
    """10개 토픽 점수 산정 & 우선순위 정렬."""

    def __init__(self, data, topic_stats, pattern_stats):
        self.data = data
        self.topic_stats = topic_stats
        self.pattern_stats = pattern_stats

    def _score_topics(self):
        """가중 점수: avg_views×0.35 + high_ratio×0.25 + bookmarks×0.20 + formula_boost×0.20"""
        scores = {}

        # 정규화를 위한 최대값 계산
        all_stats = [s for s in self.topic_stats.values() if s["count"] > 0]
        if not all_stats:
            return scores

        max_views = max(s["avg_views"] for s in all_stats) or 1
        max_bm = max(s["avg_bookmarks"] for s in all_stats) or 1

        for topic, s in self.topic_stats.items():
            if topic == "기타" or s["count"] == 0:
                continue

            # 정규화된 평균 조회수
            norm_views = s["avg_views"] / max_views

            # 고성과 비율
            high_ratio = s["high_count"] / s["count"] if s["count"] > 0 else 0

            # 정규화된 북마크
            norm_bm = s["avg_bookmarks"] / max_bm

            # 공식 부스트: 해당 주제의 최적 공식 평균 조회수
            formula = TOPIC_FORMULA_MAP.get(topic)
            formula_views = 0
            if formula and formula in self.pattern_stats:
                formula_views = self.pattern_stats[formula].get("avg_views", 0)
            max_formula = max(
                (ps.get("avg_views", 0) for ps in self.pattern_stats.values()),
                default=1,
            ) or 1
            norm_formula = formula_views / max_formula

            score = (0.35 * norm_views + 0.25 * high_ratio
                     + 0.20 * norm_bm + 0.20 * norm_formula) * 100

            scores[topic] = round(score, 1)

        return scores

    def recommend(self):
        """10개 토픽 추천 리스트 반환."""
        scores = self._score_topics()
        results = []
        used_sub_topics = set()

        for topic_name in SLOT_DISTRIBUTION:
            pool = [
                st for st in SUB_TOPICS.get(topic_name, [])
                if st not in used_sub_topics
            ]
            if not pool:
                continue

            sub_topic = random.choice(pool)
            used_sub_topics.add(sub_topic)

            formula = TOPIC_FORMULA_MAP.get(topic_name, "N가지 실용 리스트")
            score = scores.get(topic_name, 50.0)

            # 기대 성과 등급
            if score >= 80:
                expected = "최상"
            elif score >= 60:
                expected = "상"
            elif score >= 40:
                expected = "중"
            else:
                expected = "하"

            results.append({
                "rank": len(results) + 1,
                "score": score,
                "topic": topic_name,
                "sub_topic": sub_topic,
                "formula": formula,
                "expected": expected,
            })

        # 점수 내림차순 정렬 후 순위 재부여
        results.sort(key=lambda x: x["score"], reverse=True)
        for i, r in enumerate(results):
            r["rank"] = i + 1

        return results


# ─────────────────────────────────────────────
# HookGenerator (에이전트 A)
# ─────────────────────────────────────────────

def _wa_gwa(word):
    """한글 받침 유무에 따라 '와'/'과' 반환."""
    if not word:
        return "와"
    last = word[-1]
    code = ord(last)
    # 한글 유니코드 범위: 0xAC00 ~ 0xD7A3
    if 0xAC00 <= code <= 0xD7A3:
        # 받침 있으면 '과', 없으면 '와'
        if (code - 0xAC00) % 28 != 0:
            return "과"
    return "와"


class HookGenerator:
    """Hook(첫 문장) 생성. 70% 템플릿 슬롯채움, 30% 실제 고성과 Hook 변형."""

    TEMPLATES = {
        "🚨경고/긴급+실용정보": [
            "{emoji_warning}이거 안해두면 {device} 큰일남",
            "{emoji_warning}{device} 유저 필수 설정 {n}가지{emoji_warning}",
            "⚠️{topic_short} 절대 안 하면 손해",
        ],
        "A와 B 대비형": [
            "{a}{wa} {b}의 차이",
            "{a}{wa} {b}를 구분하는 결정적 차이",
            "오래가는 {a_category} vs 금방 끊기는 {a_category}",
        ],
        "N가지 실용 리스트": [
            "알아두면 언젠간 써먹는 {topic_short} {n}가지",
            "{topic_short} 총정리 {emoji_list}",
            "반드시 알아야 할 {topic_short} {n}가지",
        ],
        "손실회피+정부혜택": [
            "🧓{age}살 전에 신청 안하면 못 받는 정부 지원 혜택",
            "{emoji_warning}대부분 모르는 {benefit_target} 지원금 {n}가지",
            "지금 안 하면 못 받는 {topic_short} 총정리",
        ],
    }

    # 주제 → 짧은 표현 매핑
    TOPIC_SHORT = {
        "기술/유틸 팁": ["폰 설정", "스마트폰 기능", "아이폰 설정", "갤럭시 설정"],
        "인간관계/심리": ["인간관계", "관계", "대화법", "사람 보는 법"],
        "정부혜택/제도": ["정부 혜택", "지원금", "정부 지원"],
        "금융/투자": ["재테크", "돈 관리", "투자 상식", "금융 지식"],
    }

    def __init__(self, data, pattern_stats):
        self.data = data
        self.pattern_stats = pattern_stats

    def generate(self, topic_info):
        """토픽 정보를 받아 Hook 문장 생성. 최대 3회 재시도."""
        topic = topic_info["topic"]
        formula = topic_info["formula"]
        sub_topic = topic_info["sub_topic"]

        for _ in range(3):
            if random.random() < 0.7:
                hook = self._fill_template(formula, topic, sub_topic)
            else:
                hook = self._adapt_hook(topic, sub_topic)

            # 50자 이내로 자르기
            if len(hook) > 50:
                hook = hook[:48] + ".."

            # 검증
            labels = classify_hook(hook)
            if "unclassified" not in labels:
                return hook

        # 3회 실패 시 안전한 기본 Hook
        return self._fallback_hook(formula, topic, sub_topic)

    def _fill_template(self, formula, topic, sub_topic):
        """템플릿의 슬롯을 채워서 Hook 생성."""
        templates = self.TEMPLATES.get(formula, self.TEMPLATES["N가지 실용 리스트"])
        tmpl = random.choice(templates)

        fillers = {
            "emoji_warning": random.choice(SLOT_FILLERS["emoji_warning"]),
            "emoji_list": random.choice(SLOT_FILLERS["emoji_list"]),
            "device": random.choice(SLOT_FILLERS["device"]),
            "n": random.choice(SLOT_FILLERS["n"]),
            "age": random.choice(SLOT_FILLERS["age"]),
            "benefit_target": random.choice(SLOT_FILLERS["benefit_target"]),
            "topic_short": random.choice(self.TOPIC_SHORT.get(topic, ["생활 꿀팁"])),
        }

        # A와 B 대비형 전용
        if formula == "A와 B 대비형":
            pair = random.choice(SLOT_FILLERS["a_b_pairs"])
            fillers["a"] = pair[0]
            fillers["b"] = pair[1]
            fillers["wa"] = _wa_gwa(pair[0])
            # a_category: 첫 번째 항목에서 공통 카테고리 추출
            fillers["a_category"] = pair[0].replace("가는 ", "").replace("있는 ", "")

        try:
            return tmpl.format(**fillers)
        except KeyError:
            return tmpl

    def _adapt_hook(self, topic, sub_topic):
        """실제 고성과 Hook의 주제어만 교체하여 변형."""
        hooks = MAPUING_HIGH_HOOKS.get(topic, [])
        if not hooks:
            # 다른 주제에서 가져와서 주제어 교체
            all_hooks = []
            for h_list in MAPUING_HIGH_HOOKS.values():
                all_hooks.extend(h_list)
            hooks = all_hooks

        base = random.choice(hooks)

        # 간단한 주제어 교체
        short_topics = self.TOPIC_SHORT.get(topic, ["생활 꿀팁"])
        replacement = random.choice(short_topics)

        # 기존 주제 키워드를 새 키워드로 치환
        result = base
        for old_kw in ["아이폰", "갤럭시", "스마트폰", "폰"]:
            if old_kw in result and topic != "기술/유틸 팁":
                result = result.replace(old_kw, replacement, 1)
                break

        return result

    def _fallback_hook(self, formula, topic, sub_topic):
        """안전한 기본 Hook (반드시 분류 가능)."""
        fallbacks = {
            "🚨경고/긴급+실용정보": f"🚨모르면 손해인 {random.choice(self.TOPIC_SHORT.get(topic, ['꿀팁']))} {random.choice(SLOT_FILLERS['n_small'])}가지",
            "A와 B 대비형": (lambda p=random.choice(SLOT_FILLERS['a_b_pairs']): f"{p[0]}{_wa_gwa(p[0])} {p[1]}의 차이")(),
            "N가지 실용 리스트": f"반드시 알아야 할 {random.choice(self.TOPIC_SHORT.get(topic, ['상식']))} {random.choice(SLOT_FILLERS['n_small'])}가지",
            "손실회피+정부혜택": f"🚨{random.choice(SLOT_FILLERS['age'])}살 전에 꼭 신청해야 할 정부 지원 혜택",
        }
        return fallbacks.get(formula, f"알아두면 도움되는 {sub_topic}")


# ─────────────────────────────────────────────
# BodyGenerator (에이전트 B)
# ─────────────────────────────────────────────

class BodyGenerator:
    """Hook 이어받아 Body 생성. 4개 공식별 전용 빌더."""

    def __init__(self, data, pattern_stats):
        self.data = data
        self.pattern_stats = pattern_stats

    def generate(self, hook, topic_info):
        """Hook과 토픽 정보를 받아 완성 트윗(Hook+Body) 생성."""
        formula = topic_info["formula"]
        topic = topic_info["topic"]
        sub_topic = topic_info["sub_topic"]

        builders = {
            "🚨경고/긴급+실용정보": self._build_warning_practical,
            "A와 B 대비형": self._build_contrast,
            "N가지 실용 리스트": self._build_list,
            "손실회피+정부혜택": self._build_loss_aversion,
        }

        builder = builders.get(formula, self._build_list)
        body = builder(topic, sub_topic)

        return f"{hook}\n\n{body}"

    def _build_warning_practical(self, topic, sub_topic):
        """공식1: 🚨경고+번호리스트+화살표. 마푸잉 #1, #2, #7, #13 참조."""
        content = BODY_CONTENT.get("기술/유틸 팁", {})
        items = content.get("warning_items", [])

        if not items:
            return self._build_list(topic, sub_topic)

        selected = random.sample(items, min(random.choice([3, 4, 5]), len(items)))

        lines = ["지금 바로 확인하세요", ""]
        for i, (name, path, effect) in enumerate(selected, 1):
            lines.append(f"{i}. {name}")
            lines.append(f"{path}")
            lines.append(f"✅{effect}")
            lines.append("")

        return "\n".join(lines).strip()

    def _build_contrast(self, topic, sub_topic):
        """공식2: A항목N개 + B항목N개. 마푸잉 #3, #11, #15 참조."""
        content = BODY_CONTENT.get("인간관계/심리", {})

        positives = content.get("positive_traits", [])
        negatives = content.get("negative_traits", [])
        labels = content.get("contrast_labels", [])

        if not positives or not negatives:
            return self._build_list(topic, sub_topic)

        # Hook에서 적절한 라벨 쌍 선택
        label_pair = random.choice(labels)
        neg_label, pos_label = label_pair[1], label_pair[0]

        n = random.choice([4, 5, 6])
        neg_items = random.sample(negatives, min(n, len(negatives)))
        pos_items = random.sample(positives, min(n, len(positives)))

        lines = [neg_label]
        for i, item in enumerate(neg_items, 1):
            lines.append(f"{i}. {item}")
        lines.append("")
        lines.append(pos_label)
        for i, item in enumerate(pos_items, 1):
            lines.append(f"{i}. {item}")

        return "\n".join(lines)

    def _build_list(self, topic, sub_topic):
        """공식3: N. 항목 → 설명. 마푸잉 #6, #10, #12 참조."""
        # 주제에 맞는 콘텐츠 풀 선택
        if topic == "기술/유틸 팁":
            items = BODY_CONTENT.get("기술/유틸 팁", {}).get("list_items", [])
        elif topic == "금융/투자":
            items = BODY_CONTENT.get("금융/투자", {}).get("list_items", [])
        else:
            # 범용 리스트
            items = BODY_CONTENT.get("기술/유틸 팁", {}).get("list_items", [])

        if not items:
            return "1. 항목 1\n→ 설명\n\n2. 항목 2\n→ 설명"

        n = random.choice([5, 6, 7])
        selected = random.sample(items, min(n, len(items)))

        lines = []
        for i, (name, desc) in enumerate(selected, 1):
            lines.append(f"{i}. {name}")
            lines.append(f"→ {desc}")
            lines.append("")

        return "\n".join(lines).strip()

    def _build_loss_aversion(self, topic, sub_topic):
        """공식4: 연령대별 혜택 나열. 마푸잉 #5 참조."""
        benefits = BODY_CONTENT.get("정부혜택/제도", {}).get("age_benefits", {})

        if not benefits:
            return self._build_list(topic, sub_topic)

        # 2~3개 연령대 선택
        ages = random.sample(list(benefits.keys()), min(3, len(benefits)))

        lines = []
        for age in ages:
            lines.append(f"📌 {age}")
            lines.append("")
            age_items = benefits[age]
            selected = random.sample(age_items, min(2, len(age_items)))
            for name, detail in selected:
                lines.append(f"- {name}")
                lines.append(f"  {detail}")
            lines.append("")

        return "\n".join(lines).strip()


# ─────────────────────────────────────────────
# 출력 함수
# ─────────────────────────────────────────────

def print_topic_table(topics):
    """10개 토픽 순위표 출력."""
    print()
    print("=" * 60)
    print("📊 추천 토픽 TOP 10 (우선순위)")
    print("=" * 60)
    print()
    print(f" {'순위':^4} | {'점수':^5} | {'주제':^14} | {'세부 토픽':^28} | {'추천 공식':^20} | {'기대':^4}")
    print(f" {'─'*4} | {'─'*5} | {'─'*14} | {'─'*28} | {'─'*20} | {'─'*4}")

    for t in topics:
        print(
            f" {t['rank']:^4} | {t['score']:>5.1f} | {t['topic']:<14} | "
            f"{t['sub_topic']:<28} | {t['formula']:<20} | {t['expected']:^4}"
        )
    print()


def print_tweet(idx, topic_info, tweet_text, hook_labels, body_labels, char_count):
    """개별 트윗 포맷 출력."""
    hook_kr = ", ".join(HOOK_LABELS_KR.get(l, l) for l in hook_labels)
    body_kr = ", ".join(BODY_LABELS_KR.get(l, l) for l in body_labels)

    print(f"━━━ [#{idx}] {topic_info['topic']} | {topic_info['formula']} | 기대: {topic_info['expected']} ━━━")
    print()
    print(tweet_text)
    print()
    print(f"  Hook: {hook_kr} | Body: {body_kr} | {char_count}자")
    print()


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────

def _out(text, *streams):
    """터미널 + 파일 버퍼에 동시 출력."""
    for s in streams:
        s.write(text + "\n")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    random.seed()

    # 파일 저장용 버퍼
    buf = io.StringIO()
    out = lambda text="": _out(text, sys.stdout, buf)

    out("=" * 60)
    out("마푸잉 트윗 생성기 v1.0")
    out("=" * 60)

    # 1. 데이터 로드
    data = load_mapuing_tweets()
    orig_count = len(data["orig"])
    high_count = len(data["high_orig"])

    topic_stats = analyze_topics(data["orig"])
    pattern_stats = analyze_mapuing_unique_patterns(data["orig"])

    formula_count = len(pattern_stats)
    out(f"\n[데이터 로드] 오리지널 {orig_count}건, 고성과 {high_count}건, 공식 {formula_count}가지")

    # 2. 토픽 추천
    recommender = TopicRecommender(data, topic_stats, pattern_stats)
    topics = recommender.recommend()

    out()
    out("=" * 60)
    out("📊 추천 토픽 TOP 10 (우선순위)")
    out("=" * 60)
    out()
    out(f" {'순위':^4} | {'점수':^5} | {'주제':^14} | {'세부 토픽':^28} | {'추천 공식':^20} | {'기대':^4}")
    out(f" {'─'*4} | {'─'*5} | {'─'*14} | {'─'*28} | {'─'*20} | {'─'*4}")
    for t in topics:
        out(
            f" {t['rank']:^4} | {t['score']:>5.1f} | {t['topic']:<14} | "
            f"{t['sub_topic']:<28} | {t['formula']:<20} | {t['expected']:^4}"
        )
    out()

    # 3. 트윗 생성
    hook_gen = HookGenerator(data, pattern_stats)
    body_gen = BodyGenerator(data, pattern_stats)

    out("=" * 60)
    out("🐦 생성된 트윗 (10개)")
    out("=" * 60)
    out()

    success_count = 0
    for topic_info in topics:
        hook = hook_gen.generate(topic_info)
        tweet_text = body_gen.generate(hook, topic_info)

        hook_text = extract_hook(tweet_text)
        hook_labels = classify_hook(hook_text)
        body_labels = classify_body(tweet_text)

        text_clean = URL_RE.sub("", tweet_text).strip()
        char_count = len(text_clean)

        hook_kr = ", ".join(HOOK_LABELS_KR.get(l, l) for l in hook_labels)
        body_kr = ", ".join(BODY_LABELS_KR.get(l, l) for l in body_labels)

        out(f"━━━ [#{topic_info['rank']}] {topic_info['topic']} | {topic_info['formula']} | 기대: {topic_info['expected']} ━━━")
        out()
        out(tweet_text)
        out()
        out(f"  Hook: {hook_kr} | Body: {body_kr} | {char_count}자")
        out()

        if "unclassified" not in hook_labels:
            success_count += 1

    # 4. 요약
    out("=" * 60)
    out(f"생성 완료: {len(topics)}개 트윗")
    out(f"Hook 분류 성공률: {success_count}/{len(topics)} ({success_count/len(topics)*100:.0f}%)")
    out("=" * 60)

    # 5. 파일 저장
    save_dir = Path(__file__).parent / "생성트윗"
    save_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_path = save_dir / f"트윗_{timestamp}.txt"
    save_path.write_text(buf.getvalue(), encoding="utf-8")

    print(f"\n📁 저장 완료: {save_path}")
    buf.close()


if __name__ == "__main__":
    main()
