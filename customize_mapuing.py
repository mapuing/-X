"""
마푸잉 계정 맞춤 트윗 공식 가이드라인 생성 스크립트
- reverse_engineer_tweets.py의 분류 함수를 재사용
- 마푸잉.jsonl 데이터에서 스타일/패턴/주제 분석
- tweet_formulas.md에 섹션 8로 추가
"""

import json
import re
import sys
from pathlib import Path
from collections import defaultdict

# reverse_engineer_tweets.py에서 함수/상수 import
from reverse_engineer_tweets import (
    extract_hook, classify_hook, classify_body, calc_engagement_rate,
    analyze_group, compute_label_stats, rank_labels,
    format_number, truncate_text, get_top_examples,
    HOOK_LABELS_KR, BODY_LABELS_KR, HOOK_PATTERNS, URL_RE,
)

BASE_DIR = Path(__file__).parent
MAPUING_FILE = BASE_DIR / "x 자료" / "마푸잉.jsonl"
FORMULAS_MD = BASE_DIR / "tweet_formulas.md"
THRESHOLD = 100_000

# ─────────────────────────────────────────────
# 주제 분류 키워드
# ─────────────────────────────────────────────

TOPIC_PATTERNS = {
    "기술/유틸 팁": re.compile(
        r'(아이폰|갤럭시|스마트폰|배터리|설정|앱|카메라|스팸|차단|폰|충전|이어폰|'
        r'사생활|보안|호신|112|스마트|Wi-?Fi|블루투스|업데이트|기능|AI|PC|노트북)',
        re.IGNORECASE
    ),
    "인간관계/심리": re.compile(
        r'(관계|대화법|호감|매너|위선|자신감|허세|거짓말|가스라이팅|솔직|무례|'
        r'리더십|소극적|콤플렉스|우울|감정|눈치|사람|성격|심리|인간|대인)',
        re.IGNORECASE
    ),
    "금융/투자": re.compile(
        r'(주식|투자|코인|비트|국장|재테크|부자|돈|수익|매도|매수|ETF|배당|'
        r'폰지|경제|환율|금리|부동산|전세|월세|자산|저축|펀드|대출)',
        re.IGNORECASE
    ),
    "정부혜택/제도": re.compile(
        r'(정부|지원|혜택|신청|청년|실업급여|보조금|연금|주택청약|세금|공제|'
        r'국민내일|건보|의료|복지|보험|공공|민원|서류|증명)',
        re.IGNORECASE
    ),
}

# ─────────────────────────────────────────────
# 마푸잉 고유 패턴 정의
# ─────────────────────────────────────────────

MAPUING_PATTERNS = {
    "🚨경고/긴급+실용정보": re.compile(
        r'(🚨|⚠️|❗|‼️|긴급|주의|경고|절대|안하면|안해두면|못\s*받|손해)',
        re.IGNORECASE
    ),
    "A와 B 대비형": re.compile(
        r'(.{1,15}[와과]\s*.{1,15}\s*(차이|구분|구별|비교|vs)|'
        r'오래가는.+금방|솔직함.+무례|매너.+위선|자신감.+허세)',
        re.IGNORECASE
    ),
    "N가지 실용 리스트": re.compile(
        r'(\d+\s*가지|\d+\s*개|\d+\s*선|모음|총정리|정리)',
        re.IGNORECASE
    ),
    "손실회피+정부혜택": re.compile(
        r'(못\s*받|안\s*하면|놓치|손해|전에\s*신청|안하면\s*못).{0,30}'
        r'(정부|지원|혜택|청년|연금|보조|실업|세금|공제|보험)',
        re.IGNORECASE
    ),
}


# ─────────────────────────────────────────────
# 1. 데이터 로드
# ─────────────────────────────────────────────

def load_mapuing_tweets():
    """마푸잉.jsonl 로드 → 중복 제거, viewCount 누락 제외, orig/rt/high/low 분리."""
    seen = set()
    tweets = []

    with open(MAPUING_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            t = json.loads(line)
            t["source"] = "마푸잉"

            if "viewCount" not in t or t["viewCount"] is None:
                continue

            key = t.get("fullText", "")
            if key in seen:
                continue
            seen.add(key)
            tweets.append(t)

    orig = [t for t in tweets if not t.get("fullText", "").startswith("RT @")]
    rt = [t for t in tweets if t.get("fullText", "").startswith("RT @")]
    high_orig = [t for t in orig if t.get("viewCount", 0) >= THRESHOLD]
    low_orig = [t for t in orig if t.get("viewCount", 0) < THRESHOLD]
    high_rt = [t for t in rt if t.get("viewCount", 0) >= THRESHOLD]
    low_rt = [t for t in rt if t.get("viewCount", 0) < THRESHOLD]

    return {
        "all": tweets,
        "orig": orig,
        "rt": rt,
        "high_orig": high_orig,
        "low_orig": low_orig,
        "high_rt": high_rt,
        "low_rt": low_rt,
    }


# ─────────────────────────────────────────────
# 2. 주제별 성과 분석
# ─────────────────────────────────────────────

def classify_topic(text):
    """트윗 텍스트를 주제로 분류 (다중 레이블)."""
    labels = []
    text_clean = URL_RE.sub("", text).strip()
    for topic, pattern in TOPIC_PATTERNS.items():
        if pattern.search(text_clean):
            labels.append(topic)
    if not labels:
        labels.append("기타")
    return labels


def analyze_topics(orig_tweets):
    """주제별 트윗 수, 평균 조회수, 평균 좋아요, 평균 북마크 계산."""
    topic_data = defaultdict(list)
    for t in orig_tweets:
        text = t.get("fullText", "")
        topics = classify_topic(text)
        for topic in topics:
            topic_data[topic].append(t)

    stats = {}
    for topic, items in topic_data.items():
        n = len(items)
        avg_views = sum(t.get("viewCount", 0) for t in items) / n
        avg_likes = sum(t.get("likeCount", 0) for t in items) / n
        avg_bm = sum(t.get("bookmarkCount", 0) for t in items) / n
        high_count = sum(1 for t in items if t.get("viewCount", 0) >= THRESHOLD)
        stats[topic] = {
            "count": n,
            "high_count": high_count,
            "avg_views": round(avg_views),
            "avg_likes": round(avg_likes, 1),
            "avg_bookmarks": round(avg_bm, 1),
        }
    return stats


# ─────────────────────────────────────────────
# 3. 마푸잉 고유 패턴 분석
# ─────────────────────────────────────────────

def analyze_mapuing_unique_patterns(orig_tweets):
    """마푸잉 고유 패턴(🚨경고형, A vs B형, 리스트형, 손실회피형) 감지 및 통계."""
    pattern_data = defaultdict(list)
    for t in orig_tweets:
        text = t.get("fullText", "")
        text_clean = URL_RE.sub("", text).strip()
        for pat_name, pat_re in MAPUING_PATTERNS.items():
            if pat_re.search(text_clean):
                pattern_data[pat_name].append(t)

    stats = {}
    for pat_name, items in pattern_data.items():
        n = len(items)
        if n == 0:
            continue
        avg_views = sum(t.get("viewCount", 0) for t in items) / n
        avg_likes = sum(t.get("likeCount", 0) for t in items) / n
        avg_bm = sum(t.get("bookmarkCount", 0) for t in items) / n
        high_count = sum(1 for t in items if t.get("viewCount", 0) >= THRESHOLD)

        # 상위 3개 예시 (조회수 기준)
        top = sorted(items, key=lambda x: x.get("viewCount", 0), reverse=True)[:3]
        stats[pat_name] = {
            "count": n,
            "high_count": high_count,
            "avg_views": round(avg_views),
            "avg_likes": round(avg_likes, 1),
            "avg_bookmarks": round(avg_bm, 1),
            "top_examples": top,
        }
    return stats


# ─────────────────────────────────────────────
# 4. 전체 성공 공식 ↔ 마푸잉 매핑
# ─────────────────────────────────────────────

def map_to_global_formula(high_analyzed, low_analyzed):
    """
    전체 56개 계정 성공 공식(Hook/Body 유형)과 마푸잉 사용 빈도 대비 매핑.
    → 강점 / 미활용 / 개선 필요 분류
    """
    high_hook_stats = compute_label_stats(high_analyzed, "hook")
    low_hook_stats = compute_label_stats(low_analyzed, "hook")
    high_body_stats = compute_label_stats(high_analyzed, "body")
    low_body_stats = compute_label_stats(low_analyzed, "body")

    # Hook 매핑
    hook_mapping = []
    all_hook_labels = set(list(high_hook_stats.keys()) + list(low_hook_stats.keys()))
    for label in sorted(all_hook_labels):
        if label == "unclassified":
            continue
        h = high_hook_stats.get(label, {})
        l = low_hook_stats.get(label, {})
        h_count = h.get("count", 0)
        l_count = l.get("count", 0)
        total = h_count + l_count
        kr = HOOK_LABELS_KR.get(label, label)

        if h_count >= 2:
            status = "강점"
        elif total > 0 and h_count >= 1:
            status = "활용 중"
        elif l_count > 0 and h_count == 0:
            status = "개선 필요"
        else:
            status = "미활용"

        hook_mapping.append({
            "label": label,
            "label_kr": kr,
            "high_count": h_count,
            "low_count": l_count,
            "avg_views": h.get("avg_views", 0),
            "status": status,
        })

    # Body 매핑
    body_mapping = []
    all_body_labels = set(list(high_body_stats.keys()) + list(low_body_stats.keys()))
    for label in sorted(all_body_labels):
        if label == "unclassified":
            continue
        h = high_body_stats.get(label, {})
        l = low_body_stats.get(label, {})
        h_count = h.get("count", 0)
        l_count = l.get("count", 0)
        total = h_count + l_count
        kr = BODY_LABELS_KR.get(label, label)

        if h_count >= 2:
            status = "강점"
        elif total > 0 and h_count >= 1:
            status = "활용 중"
        elif l_count > 0 and h_count == 0:
            status = "개선 필요"
        else:
            status = "미활용"

        body_mapping.append({
            "label": label,
            "label_kr": kr,
            "high_count": h_count,
            "low_count": l_count,
            "avg_views": h.get("avg_views", 0),
            "status": status,
        })

    return hook_mapping, body_mapping, high_hook_stats, low_hook_stats, high_body_stats, low_body_stats


# ─────────────────────────────────────────────
# 5. 섹션 8 Markdown 생성
# ─────────────────────────────────────────────

def generate_section8(data, high_analyzed, low_analyzed,
                      topic_stats, pattern_stats,
                      hook_mapping, body_mapping,
                      high_hook_stats, low_hook_stats,
                      high_body_stats, low_body_stats):
    """섹션 8 전체 Markdown 생성."""

    lines = []
    def w(s=""):
        lines.append(s)

    total = len(data["all"])
    orig_count = len(data["orig"])
    rt_count = len(data["rt"])
    high_orig_count = len(data["high_orig"])
    low_orig_count = len(data["low_orig"])
    high_rt_count = len(data["high_rt"])

    # 고성과 평균 조회수
    if data["high_orig"]:
        high_avg_views = sum(t.get("viewCount", 0) for t in data["high_orig"]) // high_orig_count
    else:
        high_avg_views = 0

    # 스타일 특성 계산
    emoji_re = re.compile(r'[\U0001F300-\U0001F9FF\U00002600-\U000027BF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\u200d\ufe0f]')
    with_emoji = sum(1 for t in data["orig"] if emoji_re.search(t.get("fullText", "")))
    emoji_pct = round(with_emoji / orig_count * 100, 1) if orig_count else 0

    with_hash = sum(1 for t in data["orig"] if '#' in t.get("fullText", ""))
    hash_pct = round(with_hash / orig_count * 100, 1) if orig_count else 0

    avg_len = round(sum(len(URL_RE.sub("", t.get("fullText", "")).strip()) for t in data["orig"]) / orig_count) if orig_count else 0

    # ── 8.1 계정 프로필 요약 ──
    w("## 8. 마푸잉 계정 맞춤 가이드라인")
    w()
    w("---")
    w()
    w("### 8.1 계정 프로필 요약")
    w()
    w(f"- **총 트윗**: {total:,}건 (중복 제거 후)")
    w(f"  - 오리지널: {orig_count:,}건 ({orig_count/total*100:.1f}%)")
    w(f"  - RT: {rt_count:,}건 ({rt_count/total*100:.1f}%)")
    w(f"- **고성과 오리지널** (10만+ 조회): {high_orig_count:,}건 ({high_orig_count/orig_count*100:.1f}%)")
    w(f"  - 평균 조회수: **{high_avg_views:,}**")
    w(f"- **저성과 오리지널**: {low_orig_count:,}건")
    w(f"- **스타일 특성**:")
    w(f"  - 이모지 사용률: {emoji_pct}% (오리지널 기준)")
    w(f"  - 해시태그 사용률: {hash_pct}%")
    w(f"  - 평균 텍스트 길이: {avg_len}자 (URL 제외)")
    w()

    # ── 8.2 Hook/Body 패턴 분석 ──
    w("### 8.2 마푸잉 Hook/Body 패턴 분석")
    w()
    w("#### 고성과 vs 저성과 Hook 유형 비교")
    w()
    w("| Hook 유형 | 고성과(건) | 저성과(건) | 고성과 평균 조회수 |")
    w("|:---|:---:|:---:|:---:|")
    all_hook_labels = sorted(set(list(high_hook_stats.keys()) + list(low_hook_stats.keys())))
    for label in all_hook_labels:
        if label == "unclassified":
            continue
        h = high_hook_stats.get(label, {})
        l = low_hook_stats.get(label, {})
        kr = HOOK_LABELS_KR.get(label, label)
        h_count = h.get("count", 0)
        l_count = l.get("count", 0)
        avg_v = format_number(h.get("avg_views", 0)) if h_count > 0 else "-"
        w(f"| {kr} | {h_count} | {l_count} | {avg_v} |")
    # unclassified
    h_unc = high_hook_stats.get("unclassified", {})
    l_unc = low_hook_stats.get("unclassified", {})
    if h_unc or l_unc:
        w(f"| 분류 불가 | {h_unc.get('count', 0)} | {l_unc.get('count', 0)} | {format_number(h_unc.get('avg_views', 0)) if h_unc.get('count', 0) else '-'} |")
    w()

    w("#### 고성과 vs 저성과 Body 구조 비교")
    w()
    w("| Body 구조 | 고성과(건) | 저성과(건) | 고성과 평균 조회수 |")
    w("|:---|:---:|:---:|:---:|")
    all_body_labels = sorted(set(list(high_body_stats.keys()) + list(low_body_stats.keys())))
    for label in all_body_labels:
        if label == "unclassified":
            continue
        h = high_body_stats.get(label, {})
        l = low_body_stats.get(label, {})
        kr = BODY_LABELS_KR.get(label, label)
        h_count = h.get("count", 0)
        l_count = l.get("count", 0)
        avg_v = format_number(h.get("avg_views", 0)) if h_count > 0 else "-"
        w(f"| {kr} | {h_count} | {l_count} | {avg_v} |")
    h_unc = high_body_stats.get("unclassified", {})
    l_unc = low_body_stats.get("unclassified", {})
    if h_unc or l_unc:
        w(f"| 분류 불가 | {h_unc.get('count', 0)} | {l_unc.get('count', 0)} | {format_number(h_unc.get('avg_views', 0)) if h_unc.get('count', 0) else '-'} |")
    w()

    # 고성과 대표 예시
    if high_analyzed:
        w("#### 고성과 대표 트윗 (상위 5)")
        w()
        top5 = sorted(high_analyzed, key=lambda x: x["views"], reverse=True)[:5]
        for i, ex in enumerate(top5, 1):
            hook_kr = ", ".join(HOOK_LABELS_KR.get(l, l) for l in ex["hook_labels"])
            body_kr = ", ".join(BODY_LABELS_KR.get(l, l) for l in ex["body_labels"])
            w(f"{i}. **조회 {format_number(ex['views'])}** | 좋아요 {format_number(ex['likes'])} | 북마크 {format_number(ex['bookmarkCount'])}")
            w(f"   - Hook: {hook_kr} / Body: {body_kr}")
            w(f"   - `{truncate_text(ex['text'], 120)}`")
        w()

    # ── 8.3 주제별 성과 분석 ──
    w("### 8.3 주제별 성과 분석")
    w()
    w("| 주제 | 오리지널 수 | 고성과 수 | 평균 조회수 | 평균 좋아요 | 평균 북마크 |")
    w("|:---|:---:|:---:|:---:|:---:|:---:|")
    for topic in sorted(topic_stats.keys(), key=lambda x: topic_stats[x]["avg_views"], reverse=True):
        s = topic_stats[topic]
        w(f"| **{topic}** | {s['count']} | {s['high_count']} | {format_number(s['avg_views'])} | {format_number(int(s['avg_likes']))} | {format_number(int(s['avg_bookmarks']))} |")
    w()

    w("> **핵심 인사이트**: 기술/유틸 팁 주제가 조회수·북마크 모두 최상위. 정부혜택은 건수는 적지만 단건 효율이 매우 높음.")
    w()

    # ── 8.4 전체 성공 공식 ↔ 마푸잉 매핑 ──
    w("### 8.4 전체 성공 공식 ↔ 마푸잉 매핑")
    w()

    w("#### Hook 유형 매핑")
    w()
    w("| Hook 유형 | 마푸잉 고성과 | 마푸잉 저성과 | 상태 |")
    w("|:---|:---:|:---:|:---|")
    for m in sorted(hook_mapping, key=lambda x: x["high_count"], reverse=True):
        status_emoji = {"강점": "🟢", "활용 중": "🟡", "미활용": "⚪", "개선 필요": "🔴"}.get(m["status"], "")
        w(f"| {m['label_kr']} | {m['high_count']} | {m['low_count']} | {status_emoji} {m['status']} |")
    w()

    w("#### Body 구조 매핑")
    w()
    w("| Body 구조 | 마푸잉 고성과 | 마푸잉 저성과 | 상태 |")
    w("|:---|:---:|:---:|:---|")
    for m in sorted(body_mapping, key=lambda x: x["high_count"], reverse=True):
        status_emoji = {"강점": "🟢", "활용 중": "🟡", "미활용": "⚪", "개선 필요": "🔴"}.get(m["status"], "")
        w(f"| {m['label_kr']} | {m['high_count']} | {m['low_count']} | {status_emoji} {m['status']} |")
    w()

    # ── 8.5 마푸잉 맞춤 트윗 공식 4가지 ──
    w("### 8.5 마푸잉 맞춤 트윗 공식 4가지")
    w()

    for pat_name in ["🚨경고/긴급+실용정보", "A와 B 대비형", "N가지 실용 리스트", "손실회피+정부혜택"]:
        ps = pattern_stats.get(pat_name, {})
        formula_idx = list(MAPUING_PATTERNS.keys()).index(pat_name) + 1

        w(f"#### 공식 {formula_idx}: {pat_name}")
        w()
        if ps:
            w(f"- **해당 트윗 수**: {ps['count']}건 (고성과 {ps['high_count']}건)")
            w(f"- **평균 조회수**: {format_number(ps['avg_views'])}")
            w(f"- **평균 좋아요**: {format_number(int(ps['avg_likes']))}")
            w(f"- **평균 북마크**: {format_number(int(ps['avg_bookmarks']))}")
        else:
            w("- 데이터 없음 (미활용 패턴)")
        w()

        # 공식 템플릿
        templates = _get_mapuing_template(pat_name)
        w("**작성 공식:**")
        w()
        for tmpl in templates:
            w(f"- `{tmpl}`")
        w()

        # 실제 예시
        if ps and ps.get("top_examples"):
            w("**마푸잉 실제 고성과 예시:**")
            w()
            for j, ex in enumerate(ps["top_examples"], 1):
                text = ex.get("fullText", "")
                views = ex.get("viewCount", 0)
                likes = ex.get("likeCount", 0)
                bm = ex.get("bookmarkCount", 0)
                w(f"{j}. (조회 {format_number(views)}, 좋아요 {format_number(likes)}, 북마크 {format_number(bm)}) `{truncate_text(text, 120)}`")
            w()

        w("---")
        w()

    # ── 8.6 마푸잉 맞춤 체크리스트 ──
    w("### 8.6 마푸잉 맞춤 체크리스트")
    w()

    w("#### Hook 체크리스트")
    w()
    w("- [ ] 🚨 또는 ⚠️ 이모지로 시작하여 **긴급/경고** 분위기를 연출했는가?")
    w("- [ ] 첫 줄에 **\"안하면 손해\"**, **\"모르면 손해\"** 류의 손실회피 표현이 있는가?")
    w("- [ ] **숫자 + 실용 정보** 조합인가? (\"N가지\", \"설정 N개\")")
    w("- [ ] **A와 B 차이** 형식으로 호기심을 자극하는가?")
    w("- [ ] Hook만으로 \"이건 저장해야 해\" 라는 느낌이 드는가?")
    w()

    w("#### Body 체크리스트")
    w()
    w("- [ ] **번호 리스트**(1. 2. 3.) 또는 **화살표 흐름**(→)으로 구조화했는가?")
    w("- [ ] 한 줄 50자 미만, **짧은 줄 쌓기**로 모바일 가독성을 확보했는가?")
    w("- [ ] **구체적 수치/단계**가 포함되어 북마크 가치가 있는가?")
    w("- [ ] 이모지를 소제목/구분자로 활용했는가?")
    w()

    w("#### 주제 체크리스트")
    w()
    w("- [ ] **기술/유틸 팁**: 아이폰·갤럭시 설정, 앱 활용, 보안 팁 → 최고 성과 주제")
    w("- [ ] **인간관계/심리**: A vs B 대비형과 결합 시 효과 극대화")
    w("- [ ] **정부혜택/제도**: 손실회피 프레이밍(\"안 하면 못 받는\") 필수")
    w("- [ ] **금융/투자**: 속보·시장 반응 연결 시 조회수 상승")
    w()

    w("#### 금지 패턴")
    w()

    # 저성과 패턴 동적 분석
    url_only = sum(1 for a in low_analyzed if len(URL_RE.sub("", a["text"]).strip()) < 10)
    short_no_hook = sum(1 for a in low_analyzed
                        if len(URL_RE.sub("", a["text"]).strip()) < 30
                        and "unclassified" in a["hook_labels"])
    link_share = sum(1 for a in low_analyzed
                     if URL_RE.search(a["text"])
                     and len(URL_RE.sub("", a["text"]).strip()) < 50)

    w(f"- **URL만 던지기**: 저성과 중 {link_share}건 ({round(link_share/len(low_analyzed)*100, 1) if low_analyzed else 0}%) — 텍스트 50자 미만+URL")
    w(f"- **구조 없는 초단문**: 저성과 중 {short_no_hook}건 — Hook 없는 30자 미만")
    w(f"- **텍스트 없는 링크**: 저성과 중 {url_only}건 — 텍스트 10자 미만")
    w("- **@멘션 시작**: 노출 제한으로 조회수 급감")
    w("- **해시태그 남발**: 마푸잉 스타일과 불일치 (현재 사용률 극히 낮음)")
    w()

    # ── 8.7 주간 콘텐츠 믹스 권장안 ──
    w("### 8.7 주간 콘텐츠 믹스 권장안")
    w()
    w("마푸잉 고성과 패턴을 기반으로 한 주간 7개 트윗 믹스:")
    w()
    w("| 요일 | 공식 | 주제 예시 | 기대 효과 |")
    w("|:---|:---|:---|:---|")
    w("| 월 | 🚨경고/긴급+실용정보 | 아이폰/갤럭시 설정 팁 | 높은 조회수+북마크 |")
    w("| 화 | A와 B 대비형 | 인간관계/심리 비교 | 높은 좋아요+공감RT |")
    w("| 수 | N가지 실용 리스트 | 생활 꿀팁/건강 | 북마크 유도 |")
    w("| 목 | 🚨경고/긴급+실용정보 | 보안/사생활 보호 | 조회수 극대화 |")
    w("| 금 | 손실회피+정부혜택 | 정부지원/세금/연금 | 고북마크+공유 |")
    w("| 토 | A와 B 대비형 | 자기계발/성격 유형 | 주말 바이럴 |")
    w("| 일 | N가지 실용 리스트 | 주간 베스트/종합 정리 | 꾸준한 인게이지먼트 |")
    w()

    w("> **핵심 원칙**: 모든 트윗에 🚨/⚠️/📌 이모지 Hook + 번호 리스트 Body + 실용 정보 조합을 기본으로 하되, A vs B 대비형과 손실회피형을 주 2회씩 믹스하여 다양성 확보.")
    w()

    return "\n".join(lines)


def _get_mapuing_template(pattern_name):
    """마푸잉 맞춤 공식별 작성 템플릿."""
    templates = {
        "🚨경고/긴급+실용정보": [
            "🚨이거 안해두면 [기기] [기능] 손해",
            "🚨[대상] 필수 설정 [N]가지🚨",
            "⚠️[주제] 절대 [하면 안 되는/해야 하는] 것",
        ],
        "A와 B 대비형": [
            "[A]와 [B]의 차이",
            "오래가는 [관계] vs 금방 끊기는 [관계]",
            "[긍정 특성]과 [부정 특성]을 구분하는 결정적 차이",
        ],
        "N가지 실용 리스트": [
            "알아두면 언젠간 써먹는 [주제] [N]가지",
            "[주제] 총정리 [이모지]",
            "[주제] 모음 📸/🔧/💡",
        ],
        "손실회피+정부혜택": [
            "🧓[N]살 전에 신청 안하면 못 받는 정부 지원 혜택",
            "[기한] 전에 꼭 해야 하는 [혜택] 신청",
            "🚨대부분 모르는 [대상] 지원금 [N]가지",
        ],
    }
    return templates.get(pattern_name, ["(템플릿 미정의)"])


# ─────────────────────────────────────────────
# 6. tweet_formulas.md에 append
# ─────────────────────────────────────────────

def append_to_formulas(section8_md):
    """tweet_formulas.md에 섹션 8 추가. 기존 섹션 8이 있으면 교체."""
    content = FORMULAS_MD.read_text(encoding="utf-8")

    # 기존 섹션 8 찾기: "## 8." 으로 시작하는 부분부터 끝까지 제거
    marker = "## 8. 마푸잉 계정 맞춤 가이드라인"
    if marker in content:
        idx = content.index(marker)
        # 마커 앞의 내용만 유지 (trailing newline 포함)
        content = content[:idx].rstrip() + "\n\n"
        print("  기존 섹션 8 발견 → 교체합니다.")
    else:
        # 끝에 개행 보장
        content = content.rstrip() + "\n\n"
        print("  기존 섹션 8 없음 → 새로 추가합니다.")

    content += section8_md

    FORMULAS_MD.write_text(content, encoding="utf-8")
    print(f"  저장 완료: {FORMULAS_MD.name}")


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────

def main():
    sys.stdout.reconfigure(encoding="utf-8")

    print("=" * 60)
    print("마푸잉 계정 맞춤 트윗 공식 가이드라인 생성")
    print("=" * 60)

    # 1. 데이터 로드
    print("\n[1] 마푸잉 데이터 로드 중...")
    data = load_mapuing_tweets()
    print(f"  총 트윗: {len(data['all']):,}건")
    print(f"  오리지널: {len(data['orig']):,}건 ({len(data['orig'])/len(data['all'])*100:.1f}%)")
    print(f"  RT: {len(data['rt']):,}건 ({len(data['rt'])/len(data['all'])*100:.1f}%)")
    print(f"  고성과 오리지널 (10만+): {len(data['high_orig']):,}건")
    print(f"  저성과 오리지널: {len(data['low_orig']):,}건")

    # 2. Hook/Body 분류
    print("\n[2] Hook/Body 패턴 분류 중...")
    high_analyzed = analyze_group(data["high_orig"])
    low_analyzed = analyze_group(data["low_orig"])
    print(f"  고성과 분류 완료: {len(high_analyzed)}건")
    print(f"  저성과 분류 완료: {len(low_analyzed)}건")

    # 3. 주제별 분석
    print("\n[3] 주제별 성과 분석 중...")
    topic_stats = analyze_topics(data["orig"])
    for topic in sorted(topic_stats.keys(), key=lambda x: topic_stats[x]["avg_views"], reverse=True):
        s = topic_stats[topic]
        print(f"  {topic}: {s['count']}건 (고성과 {s['high_count']}건, 평균조회 {s['avg_views']:,})")

    # 4. 마푸잉 고유 패턴 분석
    print("\n[4] 마푸잉 고유 패턴 분석 중...")
    pattern_stats = analyze_mapuing_unique_patterns(data["orig"])
    for pat_name, ps in pattern_stats.items():
        print(f"  {pat_name}: {ps['count']}건 (고성과 {ps['high_count']}건, 평균조회 {ps['avg_views']:,})")

    # 5. 전체 공식 매핑
    print("\n[5] 전체 성공 공식 ↔ 마푸잉 매핑 중...")
    hook_mapping, body_mapping, h_hook, l_hook, h_body, l_body = map_to_global_formula(
        high_analyzed, low_analyzed
    )
    for m in hook_mapping:
        print(f"  Hook [{m['status']}] {m['label_kr']}: 고{m['high_count']} 저{m['low_count']}")
    for m in body_mapping:
        print(f"  Body [{m['status']}] {m['label_kr']}: 고{m['high_count']} 저{m['low_count']}")

    # 6. 섹션 8 생성
    print("\n[6] 섹션 8 Markdown 생성 중...")
    section8 = generate_section8(
        data, high_analyzed, low_analyzed,
        topic_stats, pattern_stats,
        hook_mapping, body_mapping,
        h_hook, l_hook, h_body, l_body,
    )
    print(f"  생성 완료: {len(section8):,}자")

    # 7. tweet_formulas.md에 추가
    print("\n[7] tweet_formulas.md에 섹션 8 추가 중...")
    append_to_formulas(section8)

    print(f"\n{'=' * 60}")
    print("마푸잉 맞춤 가이드라인 생성 완료!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
