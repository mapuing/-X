"""
마푸잉 트윗 생성 파이프라인 v2.0
- tweet_generator.py의 TopicRecommender를 4배치 호출 → 80개 후보
- topic_quality_cache.json 기반 품질 필터링
- tweet_formulas_v2.md의 Lift 데이터로 최적 Hook×Body 추천
- 상위 20개 출력
- v2.0: 번호 선택 → Claude API로 트윗 초안 자동 생성
"""

import json
import os
import sys
import io
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from tweet_generator import TopicRecommender, FRAMINGS


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

# ─────────────────────────────────────────────
# Hook/Body 가이드 (Claude API 시스템 프롬프트용)
# ─────────────────────────────────────────────

HOOK_GUIDE = {
    "fact_curiosity": {
        "desc": "독자의 호기심을 자극하는 사실이나 질문으로 시작",
        "templates": ["[주제]하는 이유", "[질문]?", "[주제] [N]가지"],
        "examples": [
            "노르웨이가 한국을 특별 대우하는 이유 🐟",
            "인생에서 기복이 중요한 이유를 보여주는 최고의 사례",
        ],
    },
    "practical_tip": {
        "desc": "바로 쓸 수 있는 실용적 정보 제공",
        "templates": ["[주제] 꿀팁", "[주제] 정리", "[주제]하는 법"],
        "examples": [
            "여론조사 전화 안오게 하는법",
            "직장에서 호감도 올리는 말버릇들",
        ],
    },
    "relatable_target": {
        "desc": "특정 사람 유형을 묘사해 공감 유도",
        "templates": ["[대상]하는 사람", "[질문]?", "절대 [행동]하면 안 되는 이유"],
        "examples": [
            "승모근 잔뜩 힘줘 치솟아 있는 사람",
            "가장 현명하게 직장생활 하는 사람 5",
        ],
    },
    "warning_loss": {
        "desc": "손실 회피 심리를 자극하는 경고성 Hook",
        "templates": ["절대 [행동]하면 안 되는 이유", "[대상]하는 사람", "조심해야 하는 [대상]"],
        "examples": [
            "[ 장례식 가서 하면 안 되는 행동들 ]",
            "조심해야 하는 사람은",
        ],
    },
    "challenge": {
        "desc": "퀴즈, 투표, 도전 등 독자 참여 유도",
        "templates": ["[질문]?", "이거 [N]초 안에 풀면 상위 [N]%", "[주제] 퀴즈"],
        "examples": [
            "성인 대다수가 틀렸다는 문제 한 번 풀어보실래요?",
        ],
    },
    "credibility": {
        "desc": "현직/전문가 입장에서 내부 정보 공개",
        "templates": ["현직 [직업]이 알려주는 [주제]", "[N]년차 [직업]의 [주제]"],
        "examples": [
            "성형외과 현직이 말하는",
            "정신과 의사가 말하는",
        ],
    },
    "narrative_hook": {
        "desc": "이야기의 시작으로 궁금증 유발",
        "templates": ["[상황]했는데 [결과]", "[상황]하다가 [전개]"],
        "examples": [
            "어제 밤에 집 가다가 진짜 심장 떨어지는 줄 앎;;",
        ],
    },
    "question_poll": {
        "desc": "의견을 묻는 질문으로 댓글 유도",
        "templates": ["[질문]?", "[A] vs [B] 뭐가 맞음?"],
        "examples": [
            "입금 한다vs안한다",
        ],
    },
}

BODY_GUIDE = {
    "arrow_flow": {
        "structure": "Hook → 원인 → 화살표(→) → 과정 → 화살표(→) → 결과",
        "example_snippet": "눈 건조하다 → 오메가 3\n생리통 심하다 → 오메가 3\n여드름 심하다 → 오메가 3",
    },
    "numbered_list": {
        "structure": "Hook → 브릿지 문장 → 번호 리스트 (1. 2. 3. ...) → 마무리",
        "example_snippet": "1. 알겠습니다 ➡ 확인했습니다.\n2. 죄송합니다 ➡ 개선하겠습니다.\n3. 안 됩니다 ➡ 방법을 찾아보겠습니다.",
    },
    "short_line_stack": {
        "structure": "Hook → 짧은 문장 1 → 짧은 문장 2 → ... → 짧은 마무리",
        "example_snippet": "항상 어깨 긴장 달고사는 사람\n날개뼈 뻐근한 사람\n이거 자기 전에 하래... 효과 미쳤대",
    },
    "threaded_narrative": {
        "structure": "Hook → 배경 설명 → 사건 전개 → 클라이맥스 → 결론/교훈",
        "example_snippet": "마농이 건강에 집중하기 위해 그룹 활동 임시 중단...\n\"서로 솔직하고 충분한 대화를 나눈 끝에 내린 결정\"",
    },
    "one_liner_media": {
        "structure": "짧은 캡션/코멘트 + 이미지/영상 첨부 (텍스트는 짧게)",
        "example_snippet": "한국인만 알아 본다는 중국산 김치!\n정답은?? 🤔",
    },
    "general_body": {
        "structure": "Hook → 자유 형식 본문 (특정 구조 없음)",
        "example_snippet": "요새 핫한 성수동 카페라는데...음..\n내 머릿속으론 이해가 안된다..\n바닥을 핥아서 먹으라는건가..?",
    },
    "comparison_table": {
        "structure": "Hook → A 설명 → vs → B 설명 → 결론/질문",
        "example_snippet": "진짜 자신감 vs 나르시시즘\n- 진짜: 남의 성공도 축하\n- 가짜: 남의 성공에 위협 느낌",
    },
    "dialogue_form": {
        "structure": "A: 대사 → B: 대사 → 반복 → 펀치라인",
        "example_snippet": "나: 오늘 운동 가야지\n뇌: 내일 하자\n나: 그래 내일 하자\n(3개월째 반복)",
    },
}

# 프레이밍 접두사 (중복 제거용) — tweet_generator.FRAMINGS에서 자동 파생
_FRAMING_PREFIXES = [p for prefixes in FRAMINGS.values() for p in prefixes] + ["[ ", "< "]


# ─────────────────────────────────────────────
# 품질 캐시
# ─────────────────────────────────────────────

def load_quality_cache():
    """topic_quality_cache.json 로드. 없으면 빈 dict."""
    cache_path = Path(__file__).parent / "topic_quality_cache.json"
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _flatten_cache(cache):
    """중첩 캐시를 (item, score) 플랫 리스트로 변환 (1회만 호출)."""
    return [(item, score) for pool in cache.values()
            for item, score in pool.items() if len(item) >= 2]


def _get_quality_from_cache(topic_text, flat_cache):
    """주제 텍스트에서 소재풀 항목을 매칭하여 품질 점수 반환.

    여러 소재가 매칭되면 최저 점수 사용 (약한 소재가 전체 품질 결정).
    매칭 없으면 기본 3.
    """
    found = [score for item, score in flat_cache if item in topic_text]
    return min(found) if found else 3


# ─────────────────────────────────────────────
# Hook × Body 추천
# ─────────────────────────────────────────────

_DEFAULT_COMBO = {
    "hook": "fact_curiosity", "body": "short_line_stack", "lift": 2.7,
    "hook_kr": HOOK_NAMES["fact_curiosity"],
    "body_kr": BODY_NAMES["short_line_stack"],
}


def _compute_combo(topic_type):
    """주제 유형에 대해 최적 Hook×Body 조합 계산."""
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
    return best or _DEFAULT_COMBO


# 7개 유형에 대해 사전 계산 (suggest_combo 80회 반복 호출 방지)
_COMBO_CACHE = {t: _compute_combo(t) for t in TYPE_TO_HOOKS}


def suggest_combo(topic_type):
    """주제 유형에 대해 최적 Hook×Body 조합 추천. 항상 dict 반환."""
    return _COMBO_CACHE.get(topic_type, _DEFAULT_COMBO)


# ─────────────────────────────────────────────
# 점수 산출
# ─────────────────────────────────────────────

def score_topic(topic_dict, cache):
    """품질 캐시(1-5) × 1.5 + 최적 Lift = 종합 점수. (combo, quality)도 반환."""
    quality = _get_quality_from_cache(topic_dict["topic"], cache)
    combo = suggest_combo(topic_dict["type"])
    score = quality * 1.5 + combo["lift"]
    return score, combo, quality


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

# ─────────────────────────────────────────────
# Claude API 트윗 초안 생성
# ─────────────────────────────────────────────

def _build_system_prompt(hook_type, body_type):
    """선택된 Hook×Body 조합에 맞는 시스템 프롬프트 구성."""
    hook = HOOK_GUIDE.get(hook_type, HOOK_GUIDE["fact_curiosity"])
    body = BODY_GUIDE.get(body_type, BODY_GUIDE["short_line_stack"])
    hook_name = HOOK_NAMES.get(hook_type, hook_type) + "형"
    body_name = BODY_NAMES.get(body_type, body_type) + "형"

    return f"""당신은 한국 X(트위터)에서 바이럴 트윗을 작성하는 전문가입니다.

## 작성 규칙

1. **Hook (첫 줄)**: {hook_name} 스타일
   - 설명: {hook['desc']}
   - 템플릿: {', '.join(hook['templates'])}
   - 참고 예시: {' / '.join(hook['examples'])}

2. **Body (본문)**: {body_name} 구조
   - 구조: {body['structure']}
   - 참고:
{body['example_snippet']}

3. **길이**: 151~280자 (이 구간 고성과율 12.9%)
4. **줄 길이**: 한 줄 50자 이내 (모바일 최적화)
5. **해시태그**: 최대 2개, 없어도 됨
6. **톤**: 한국 트위터 구어체. 자연스럽고 캐주얼하게. '~입니다' 체 금지.
7. **금지**: 영어 남발, 과도한 이모지, 광고성 문구, 뻔한 클리셰

## 출력 형식

정확히 3개의 트윗 초안을 작성하세요.
각 초안은 "---" 구분선으로 구분합니다.
초안 번호나 설명 없이 트윗 텍스트만 출력하세요."""


def generate_drafts(topic_dict):
    """Claude API로 트윗 초안 3개 생성. 반환: [(초안, 체크결과), ...]"""
    try:
        import anthropic
    except ImportError:
        print("\n❌ anthropic 패키지가 없습니다. 설치: python -m pip install anthropic")
        return []

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("\n❌ ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")
        print("   설정 방법: export ANTHROPIC_API_KEY='your-key-here'")
        return []

    combo = topic_dict.get("combo", _DEFAULT_COMBO)

    system = _build_system_prompt(combo["hook"], combo["body"])
    user_msg = f"다음 주제로 트윗 3개를 작성해주세요:\n\n주제: {topic_dict['topic']}\n유형: {topic_dict['type']}"

    client = anthropic.Anthropic()

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )

    # 텍스트 블록 추출
    text = "".join(block.text for block in response.content if block.type == "text")

    # "---" 구분선으로 초안 분리
    drafts = [d.strip() for d in text.split("---") if d.strip()]

    # 각 초안에 preflight_check 실행
    results = []
    for draft in drafts[:3]:
        issues = preflight_check(draft)
        results.append((draft, issues))

    return results


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
    flat_cache = _flatten_cache(cache)
    n = len(flat_cache)
    out(f"\n📦 품질 캐시: {'로드 완료 (' + str(n) + '개)' if flat_cache else '없음 (기본 점수 3)'}")

    # 2. 4배치 생성
    out("\n🎰 4배치 생성 중...")
    all_topics = generate_batch(4)
    out(f"   후보: {len(all_topics)}개 (중복 제거)")

    # 3. 점수 산출
    scored = []
    for t in all_topics:
        s, combo, q = score_topic(t, flat_cache)
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
    out("번호를 입력하면 Claude API로 트윗 초안을 자동 생성합니다.")
    out("q 입력 시 종료.")
    out("=" * 90)

    # 파일 저장
    save_dir = Path(__file__).parent / "생성트윗"
    save_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_path = save_dir / f"파이프라인_{ts}.txt"
    save_path.write_text(buf.getvalue(), encoding="utf-8")

    print(f"\n📁 저장 완료: {save_path}")
    buf.close()

    # ── 인터랙티브 모드 ──
    while True:
        try:
            choice = input("\n🔢 번호 입력 (q=종료): ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if choice.lower() == "q":
            break

        try:
            idx = int(choice)
        except ValueError:
            print("숫자를 입력하세요.")
            continue

        if idx < 1 or idx > len(top20):
            print(f"1~{len(top20)} 사이 번호를 입력하세요.")
            continue

        selected = top20[idx - 1]
        combo = selected.get("combo")
        combo_str = f"{combo['hook_kr']} → {combo['body_kr']}" if combo else "기본"

        print(f"\n{'─' * 60}")
        print(f"📝 선택: [{idx}] {selected['topic']}")
        print(f"   유형: {selected['type']} | 조합: {combo_str}")
        print(f"{'─' * 60}")
        print(f"\n🤖 Claude API로 초안 생성 중...\n")

        results = generate_drafts(selected)

        if not results:
            continue

        # 초안 출력
        draft_buf = io.StringIO()
        for i, (draft, issues) in enumerate(results, 1):
            header = f"── 초안 {i} ({len(draft)}자) "
            print(header + "─" * (60 - len(header)))
            print(draft)
            draft_buf.write(f"{header}{'─' * 20}\n{draft}\n\n")

            if issues:
                check_str = " | ".join(issues)
                print(f"  체크: {check_str}")
                draft_buf.write(f"  체크: {check_str}\n")
            else:
                print("  ✅ 체크리스트 통과")
                draft_buf.write("  ✅ 체크리스트 통과\n")
            print()

        # 초안 파일 저장
        draft_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        draft_path = save_dir / f"초안_{draft_ts}.txt"
        draft_content = f"주제: {selected['topic']}\n유형: {selected['type']}\n조합: {combo_str}\n\n{draft_buf.getvalue()}"
        draft_path.write_text(draft_content, encoding="utf-8")
        draft_buf.close()
        print(f"📁 초안 저장: {draft_path}")

    print("\n종료합니다.")


if __name__ == "__main__":
    main()
