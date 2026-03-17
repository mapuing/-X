"""
마푸잉 트윗 생성 파이프라인 v1.0
- tweet_generator.py의 TopicRecommender를 4배치 호출 → 80개 후보
- topic_quality_cache.json 기반 품질 필터링
- tweet_formulas_v2.md의 Lift 데이터로 최적 Hook×Body 추천
- 상위 20개 출력
"""

import json
import sys
import io
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from tweet_generator import TopicRecommender


# ─────────────────────────────────────────────
# v2 Lift 데이터 (tweet_formulas_v2.md §5 교차표)
# ─────────────────────────────────────────────

LIFT_TABLE = {
    "fact_curiosity": {
        "arrow_flow": 0, "comparison_table": 2.1, "dialogue_form": 0,
        "general_body": 3.2, "numbered_list": 4.38, "one_liner_media": 2.1,
        "short_line_stack": 2.7, "threaded_narrative": 2.9,
    },
    "practical_tip": {
        "arrow_flow": 5.1, "comparison_table": 2.8, "dialogue_form": 0,
        "general_body": 2.7, "numbered_list": 3.44, "one_liner_media": 1.3,
        "short_line_stack": 3.19, "threaded_narrative": 4.79,
    },
    "relatable_target": {
        "arrow_flow": 0, "comparison_table": 0.9, "dialogue_form": 0,
        "general_body": 2.0, "numbered_list": 3.45, "one_liner_media": 1.5,
        "short_line_stack": 3.1, "threaded_narrative": 2.6,
    },
    "warning_loss": {
        "arrow_flow": 0, "comparison_table": 1.2, "dialogue_form": 0,
        "general_body": 3.6, "numbered_list": 3.33, "one_liner_media": 1.2,
        "short_line_stack": 3.4, "threaded_narrative": 0.9,
    },
    "challenge": {
        "arrow_flow": 0, "comparison_table": 1.3, "dialogue_form": 0,
        "general_body": 0.9, "numbered_list": 0.0, "one_liner_media": 1.6,
        "short_line_stack": 1.3, "threaded_narrative": 0.0,
    },
    "credibility": {
        "arrow_flow": 0, "comparison_table": 0.0, "dialogue_form": 0,
        "general_body": 1.2, "numbered_list": 3.36, "one_liner_media": 0.7,
        "short_line_stack": 2.5, "threaded_narrative": 1.3,
    },
    "narrative_hook": {
        "arrow_flow": 0, "comparison_table": 0.7, "dialogue_form": 0,
        "general_body": 0.9, "numbered_list": 3.27, "one_liner_media": 0.9,
        "short_line_stack": 1.3, "threaded_narrative": 1.4,
    },
    "question_poll": {
        "arrow_flow": 0, "comparison_table": 0.4, "dialogue_form": 0.0,
        "general_body": 0.6, "numbered_list": 3.61, "one_liner_media": 0.9,
        "short_line_stack": 1.4, "threaded_narrative": 0.4,
    },
}

# 주제 유형 → 추천 Hook 유형
TYPE_TO_HOOKS = {
    "정보/실용": ["practical_tip", "warning_loss", "credibility"],
    "사람유형 묘사": ["relatable_target", "fact_curiosity"],
    "의외의 사실": ["fact_curiosity", "credibility"],
    "대비형": ["fact_curiosity", "relatable_target"],
    "참여/선택": ["question_poll", "challenge"],
    "논쟁/분노": ["warning_loss", "relatable_target", "question_poll"],
    "스토리/감동": ["narrative_hook", "fact_curiosity"],
}

HOOK_NAMES = {
    "fact_curiosity": "사실/호기심",
    "practical_tip": "실용 정보",
    "relatable_target": "공감/대상",
    "warning_loss": "경고/손실",
    "challenge": "참여 유도",
    "credibility": "권위/내부자",
    "narrative_hook": "서사 시작",
    "question_poll": "질문/투표",
}

BODY_NAMES = {
    "arrow_flow": "화살표 흐름",
    "numbered_list": "번호 리스트",
    "short_line_stack": "짧은 줄 쌓기",
    "threaded_narrative": "서사/스토리",
    "one_liner_media": "한 줄+미디어",
    "general_body": "일반 본문",
    "comparison_table": "비교/대비",
    "dialogue_form": "대화체",
}

# 프레이밍 접두사 (중복 제거용)
_FRAMING_PREFIXES = [
    "📌 놓치면 손해 — ",
    "의외로 — ", "알고 보면 — ",
    "솔직히 — ",
    "🚨 ", "⚠️ ", "🔥 ",
]


# ─────────────────────────────────────────────
# 품질 캐시
# ─────────────────────────────────────────────

def load_quality_cache():
    """topic_quality_cache.json 로드. 없으면 빈 dict."""
    cache_path = Path(__file__).parent / "topic_quality_cache.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    return {}


def _get_quality_from_cache(topic_text, cache):
    """주제 텍스트에서 소재풀 항목을 매칭하여 품질 점수 반환.

    여러 소재가 매칭되면 최저 점수 사용 (약한 소재가 전체 품질 결정).
    매칭 없으면 기본 3.
    """
    found = []
    for pool_items in cache.values():
        for item, score in pool_items.items():
            if len(item) >= 2 and item in topic_text:
                found.append(score)
    return min(found) if found else 3


# ─────────────────────────────────────────────
# Hook × Body 추천
# ─────────────────────────────────────────────

def suggest_combo(topic_type):
    """주제 유형에 대해 최적 Hook×Body 조합 추천."""
    hooks = TYPE_TO_HOOKS.get(topic_type, ["fact_curiosity"])
    best = None
    for hook in hooks:
        for body, lift in LIFT_TABLE.get(hook, {}).items():
            if lift > 0 and (best is None or lift > best["lift"]):
                best = {
                    "hook": hook, "body": body, "lift": lift,
                    "hook_kr": HOOK_NAMES.get(hook, hook),
                    "body_kr": BODY_NAMES.get(body, body),
                }
    return best


# ─────────────────────────────────────────────
# 점수 산출
# ─────────────────────────────────────────────

def score_topic(topic_dict, cache):
    """품질 캐시(1-5) × 1.5 + 최적 Lift = 종합 점수."""
    quality = _get_quality_from_cache(topic_dict["topic"], cache)
    combo = suggest_combo(topic_dict["type"])
    lift = combo["lift"] if combo else 1.0
    return quality * 1.5 + lift


# ─────────────────────────────────────────────
# 배치 생성
# ─────────────────────────────────────────────

def _strip_framing(topic):
    """프레이밍 접두사 제거 (중복 비교용)."""
    for prefix in _FRAMING_PREFIXES:
        if topic.startswith(prefix):
            return topic[len(prefix):]
    return topic


def generate_batch(n_batches=4):
    """TopicRecommender를 n_batches회 호출, 중복 제거 후 반환."""
    recommender = TopicRecommender()
    all_topics = []
    seen = set()
    for _ in range(n_batches):
        batch = recommender.recommend(20)
        for t in batch:
            clean = _strip_framing(t["topic"])
            if clean not in seen:
                seen.add(clean)
                all_topics.append(t)
    return all_topics


# ─────────────────────────────────────────────
# 체크리스트 (완성 트윗용)
# ─────────────────────────────────────────────

def preflight_check(text):
    """완성된 트윗 체크리스트 검증. 빈 리스트 = 통과."""
    issues = []
    lines = text.strip().split("\n")
    total = len(text)

    if total < 30:
        issues.append("❌ 30자 미만 초단문 (저성과 패턴)")
    if lines and len(lines[0]) < 10:
        issues.append("❌ Hook 10자 미만")
    for i, line in enumerate(lines):
        if len(line) > 50:
            issues.append(f"⚠️ {i+1}번째 줄 {len(line)}자 (모바일 50자 권장)")
            break
    if text.count("#") >= 3:
        issues.append("❌ 해시태그 3개 이상 (효과 없음)")
    if total > 280:
        issues.append(f"❌ 280자 초과 ({total}자)")
    elif total < 151:
        issues.append(f"💡 {total}자 (151-280자 구간 고성과율 12.9%)")

    return issues


# ─────────────────────────────────────────────
# main
# ─────────────────────────────────────────────

def main():
    sys.stdout.reconfigure(encoding="utf-8")

    buf = io.StringIO()
    def out(text=""):
        print(text)
        buf.write(text + "\n")

    out("=" * 90)
    out("마푸잉 트윗 생성 파이프라인 v1.0")
    out("4배치 생성 → 품질 필터 → 상위 20개 + Hook×Body 추천")
    out("=" * 90)

    # 1. 품질 캐시
    cache = load_quality_cache()
    n = sum(len(v) for v in cache.values()) if cache else 0
    out(f"\n📦 품질 캐시: {'로드 완료 (' + str(n) + '개)' if cache else '없음 (기본 점수 3)'}")

    # 2. 4배치 생성
    out("\n🎰 4배치 생성 중...")
    all_topics = generate_batch(4)
    out(f"   후보: {len(all_topics)}개 (중복 제거)")

    # 3. 점수 산출
    scored = []
    for t in all_topics:
        s = score_topic(t, cache)
        combo = suggest_combo(t["type"])
        q = _get_quality_from_cache(t["topic"], cache)
        scored.append({**t, "score": s, "combo": combo, "quality": q})

    scored.sort(key=lambda x: x["score"], reverse=True)

    # 4. 품질 ≤2 필터링
    filtered = [s for s in scored if s["quality"] > 2]
    top20 = filtered[:20] if len(filtered) >= 20 else scored[:20]

    # 5. 출력
    out()
    out("=" * 90)
    out("📊 상위 20개 주제 (점수순)")
    out("=" * 90)
    out()
    out(f" {'#':>2} | {'유형':^14} | {'주제':<44} | {'품질':^4} | 추천 Hook→Body (Lift)")
    out(f" {'─'*2}─┼─{'─'*14}─┼─{'─'*44}─┼─{'─'*4}─┼─{'─'*30}")

    for i, t in enumerate(top20, 1):
        tc = t.get("type_code", "?")
        tn = t["type"]
        topic = t["topic"]
        if len(topic) > 42:
            topic = topic[:39] + "..."

        q = t["quality"]
        qs = f"★{q}"

        combo = t.get("combo")
        cs = f"{combo['hook_kr']} → {combo['body_kr']} ({combo['lift']:.2f}×)" if combo else "—"

        out(f" {i:>2} | [{tc}] {tn:<9} | {topic:<44} | {qs:<4} | {cs}")

    out()

    # 유형별 분포
    counts = defaultdict(int)
    for t in top20:
        counts[t["type"]] += 1
    out(f"유형별 분포: {' / '.join(f'{k} {v}개' for k, v in counts.items())}")
    out()

    out("=" * 90)
    out("사용법:")
    out("  1. 위 번호를 골라 Claude에게 알려주세요")
    out("  2. Claude가 추천 Hook×Body 조합으로 후보를 작성합니다")
    out("  3. Hook 선택 → Body 작성 → 체크리스트 순으로 완성")
    out("=" * 90)

    # 파일 저장
    save_dir = Path(__file__).parent / "생성트윗"
    save_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_path = save_dir / f"파이프라인_{ts}.txt"
    save_path.write_text(buf.getvalue(), encoding="utf-8")

    print(f"\n📁 저장 완료: {save_path}")
    buf.close()


if __name__ == "__main__":
    main()
