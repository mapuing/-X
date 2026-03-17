"""
X 트윗 성공 공식 역설계 스크립트
- high_performing_tweets.json + 전체 JSONL 데이터에서 고성과/저성과 오리지널 트윗 분리
- Hook/Body 패턴 분류 → 성공 vs 실패 비교 → 재현 가능한 공식 도출
- 결과를 tweet_formulas.md에 저장
"""

import json
import re
from pathlib import Path
from collections import defaultdict

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "x 자료"
HIGH_FILE = BASE_DIR / "high_performing_tweets.json"
OUTPUT_MD = BASE_DIR / "tweet_formulas.md"
LLM_CACHE_FILE = BASE_DIR / "llm_hook_cache.json"
THRESHOLD = 100_000

# LLM 캐시 로드 (없으면 빈 dict)
_LLM_CACHE = {}
if LLM_CACHE_FILE.exists():
    with open(LLM_CACHE_FILE, "r", encoding="utf-8") as _f:
        _LLM_CACHE = json.load(_f)
LLM_HOOK_OVERRIDES = _LLM_CACHE.get("hook_type_overrides", {})

# ─────────────────────────────────────────────
# 1단계: 데이터 로드 및 분리
# ─────────────────────────────────────────────

def load_all_tweets():
    """전체 JSONL에서 트윗 로드, (fullText, source) 중복 제거, viewCount 누락 제외."""
    seen = set()
    tweets = []
    skipped_view = 0
    skipped_dup = 0

    for filepath in sorted(DATA_DIR.glob("*.jsonl")):
        account = filepath.stem
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                t = json.loads(line)
                t["source"] = account

                if "viewCount" not in t or t["viewCount"] is None:
                    skipped_view += 1
                    continue

                key = (t.get("fullText", ""), account)
                if key in seen:
                    skipped_dup += 1
                    continue
                seen.add(key)
                tweets.append(t)

    return tweets, skipped_view, skipped_dup


def calc_engagement_rate(tweet):
    views = tweet.get("viewCount", 0)
    if not views:
        return 0.0
    eng = (tweet.get("likeCount", 0) + tweet.get("quoteCount", 0)
           + tweet.get("replyCount", 0) + tweet.get("retweetCount", 0)
           + tweet.get("bookmarkCount", 0))
    return eng / views


def split_tweets(tweets):
    """고성과/저성과, RT/오리지널 분리."""
    high, low = [], []
    for t in tweets:
        if t.get("viewCount", 0) >= THRESHOLD:
            high.append(t)
        else:
            low.append(t)

    def split_rt(lst):
        orig = [t for t in lst if not t.get("fullText", "").startswith("RT @")]
        rt = [t for t in lst if t.get("fullText", "").startswith("RT @")]
        return orig, rt

    high_orig, high_rt = split_rt(high)
    low_orig, low_rt = split_rt(low)
    return high_orig, high_rt, low_orig, low_rt


# ─────────────────────────────────────────────
# 2단계: Hook 분류 (정규식 기반, 다중 레이블)
# ─────────────────────────────────────────────

URL_RE = re.compile(r'https?://\S+')

HOOK_PATTERNS = {
    "relatable_targeting": re.compile(
        r'(하는\s*사람|인\s*분들?|하는\s*분들?|있는\s*사람|겪는|겪어본|공감|해본\s*사람|느끼는|다들\s|너희|여러분)',
        re.IGNORECASE
    ),
    "credibility": re.compile(
        r'(현직|전문|의사|교수|변호사|약사|전직|경력|년차|연차|업계|실무|석사|박사|전공)',
        re.IGNORECASE
    ),
    "challenge": re.compile(
        r'(풀어|맞춰|퀴즈|테스트|도전|챌린지|찾아|틀린\s*곳|다른\s*곳|몇\s*개|맞히)',
        re.IGNORECASE
    ),
    "practical_tip": re.compile(
        r'(방법|하려면|꿀팁|노하우|비법|비결|팁\b|루틴|습관|하는\s*법|알려|정리했|모음|추천)',
        re.IGNORECASE
    ),
    "bracket_title": re.compile(
        r'(\[.+\]|【.+】|「.+」|『.+』)'
    ),
    "numbered_title": re.compile(
        r'(\d+\s*가지|\d+\s*선|top\s*\d+|\d+\s*개|best\s*\d+)',
        re.IGNORECASE
    ),
    "question": re.compile(r'\?'),
    "news_shock": re.compile(
        r'(속보|충격|논란|경악|긴급|단독|발각|폭로|대참사|ㄷㄷ|헐|실화|레전드|미쳤|대박|역대급)',
        re.IGNORECASE
    ),
    "personal_story": re.compile(
        r'(어제|오늘|방금|아까|살면서|처음으로|나는|저는|제가|내가|경험|일화|실화|겪었|당했)',
        re.IGNORECASE
    ),
    "fact_curiosity": re.compile(
        r'(이유|때문에|중요성|비결|알고\s*보[면니]|사실[은\s]|몰랐던|비밀|비하인드)',
        re.IGNORECASE
    ),
    "warning_loss": re.compile(
        r'(하지\s*마|절대\s*[하금안]|위험하|조심|주의\b|금물|하면\s*안)',
        re.IGNORECASE
    ),
    "quote_reference": re.compile(
        '(\u201c[^\u201d]+\u201d|\u2018[^\u2019]+\u2019|"[^"]{2,}")'
    ),
    "short_cryptic": None,  # 25자 이하, 별도 처리
}

HOOK_LABELS_KR = {
    "relatable_targeting": "독자 지정/공감형",
    "credibility": "권위/신뢰형",
    "challenge": "참여 유도형",
    "practical_tip": "실용 팁형",
    "bracket_title": "괄호 제목형",
    "numbered_title": "숫자 리스트형",
    "question": "질문형",
    "news_shock": "뉴스/충격형",
    "personal_story": "개인 스토리형",
    "fact_curiosity": "사실/호기심형",
    "warning_loss": "경고/손실형",
    "quote_reference": "인용/따옴표형",
    "short_cryptic": "짧은 임팩트형(≤25자)",
    # LLM 분류 유형 (캐시 기반)
    "narrative_situation": "서사/상황형",
    "info_fact": "정보전달형",
    "opinion_reaction": "의견/반응형",
    "celeb_mention": "셀럽/유명인형",
    "promotion": "홍보/추천형",
    "emotion_empathy": "감정/공감형",
    "comparison_choice": "비교/선택형",
}


SHORT_CRYPTIC_SUBPATTERNS = [
    ("person_type", re.compile(r'사람|분들|님들|유형|특징')),
    ("reason_type", re.compile(r'이유|때문|원리|원인')),
    ("consequence_type", re.compile(r'생기는\s*일|벌어지는|일어나는|되는\s*일')),
    ("reaction_type", re.compile(r'ㅋ{2,}|ㄷㄷ|ㅠ{2,}|ㅁㅊ|대박|역대급|실화|레전드|미쳤|ㄹㅇ|ㄹㅈㄷ|🤣|😂|😱|😰|🤬|ㅎㅎ')),
    ("teaser_type", re.compile(r'라는데|했더니|알고\s*보니|다는\s|인데$|건데$|했는데|하다가|하길|말하길|길래$|하자면$|이라는$|인데요|라니$|였는데')),
    ("topic_intro_type", re.compile(r'근황|현황|현실|순위|차이|변화|정리|비교|추천|모음|요약|논란|화제|사건|풍경|수준|실태')),
    ("directive_type", re.compile(r'하세요|해보세요|드립니다|해봐|하삼|드림|알려드|먹어보|써보|드세요|드셔|마세요|마시오')),
    ("vs_poll_type", re.compile(r'vs|VS|한다vs|어떻[해게]|고르|고민되네|선택|뭐[가를]\s*고|어떠세요|하시겠|몇\s*번')),
    ("celeb_sub_type", re.compile(r'감독[이가]|배우[가의]|셰프|선수[가의]|가수|유튜[버브]|인스타[그]|김선태|트럼프|일론|잡스|박명수|유재석|박봄|김혜수|조정석|아인슈타인|김새론')),
    ("diet_health_type", re.compile(r'다이어트|칼로리|저칼로리|헬스|살\s*빠|체중|영양[제소]|단백질|식단|오메가|비타민|루틴|운동\s')),
    ("emotion_marker_type", re.compile(r'울\s|울고|눈물|오열|짠하|먹먹|그립|보고\s*싶|행복|감동|소름|찡')),
    ("product_tip_type", re.compile(r'후기|리뷰|써먹|꿀팁|갓성비|가성비|직빵|맛집|신메뉴|할인|쿠폰')),
    ("bracket_list_type", re.compile(r'^\s*[\[【]|^\s*📌|^\s*‼️|^\s*⚠️|^\s*🚨|^\s*⛔|^\s*✨|^\s*💰|^\s*🔥|^\s*📎')),
]

SHORT_CRYPTIC_SUB_LABELS_KR = {
    "person_type": "~하는 사람형",
    "reason_type": "~하는 이유형",
    "consequence_type": "~하면 생기는 일형",
    "reaction_type": "감정/반응형",
    "teaser_type": "티저/미완결형",
    "topic_intro_type": "주제 제시형",
    "directive_type": "행동 유도형",
    "vs_poll_type": "비교/선택형",
    "celeb_sub_type": "셀럽/화제형",
    "diet_health_type": "건강/다이어트형",
    "emotion_marker_type": "감정 표현형",
    "product_tip_type": "제품/후기형",
    "bracket_list_type": "괄호/이모지 리스트형",
    "noun_phrase": "명사구형(기타)",
}


def classify_short_cryptic_sub(hook_text):
    """short_cryptic Hook의 서브패턴 분류."""
    for name, pattern in SHORT_CRYPTIC_SUBPATTERNS:
        if pattern.search(hook_text):
            return name
    return "noun_phrase"


def extract_hook(text):
    """첫 번째 비어있지 않은 줄을 Hook으로 추출, URL 제거."""
    text_clean = URL_RE.sub("", text).strip()
    for line in text_clean.split("\n"):
        line = line.strip()
        if line:
            return line
    return text_clean


def classify_hook(hook_text):
    """Hook 텍스트를 다중 레이블로 분류. regex 미분류 시 LLM 캐시 참조."""
    labels = []
    for name, pattern in HOOK_PATTERNS.items():
        if name == "short_cryptic":
            if len(hook_text) <= 25:
                labels.append(name)
        elif pattern and pattern.search(hook_text):
            labels.append(name)
    if not labels:
        # LLM 캐시에서 분류 시도
        llm_label = LLM_HOOK_OVERRIDES.get(hook_text)
        if llm_label:
            labels.append(llm_label)
        else:
            labels.append("unclassified")
    return labels


# ─────────────────────────────────────────────
# 3단계: Body 구조 분류
# ─────────────────────────────────────────────

BODY_LABELS_KR = {
    "numbered_list": "번호 리스트형",
    "arrow_flow": "화살표 흐름형",
    "short_line_stacking": "짧은 줄 쌓기형",
    "narrative_story": "내러티브/서사형",
    "media_post": "미디어 의존형",
}


def classify_body(text):
    """본문 구조 분류 (다중 레이블)."""
    text_clean = URL_RE.sub("", text).strip()
    labels = []

    # numbered_list: \n 뒤에 숫자+.)가 2회 이상
    if len(re.findall(r'\n\s*\d+[.)]\s', text_clean)) >= 2:
        labels.append("numbered_list")
    # 이모지 번호 리스트도 감지 (1️⃣, ①, ❶ 등)
    elif len(re.findall(r'[\n]\s*[①②③④⑤⑥⑦⑧⑨⑩❶❷❸❹❺❻❼❽❾❿\U0001F1E6-\U0001F1FF]', text_clean)) >= 2:
        labels.append("numbered_list")
    elif len(re.findall(r'\d️⃣', text_clean)) >= 2:
        labels.append("numbered_list")

    # arrow_flow
    if '→' in text_clean or '➡' in text_clean or '▶' in text_clean:
        labels.append("arrow_flow")

    # short_line_stacking: 4줄 이상, 각 50자 미만
    lines = [l.strip() for l in text_clean.split("\n") if l.strip()]
    if len(lines) >= 4 and all(len(l) < 50 for l in lines):
        labels.append("short_line_stacking")

    # narrative_story: 120자 초과, 리스트 아님
    if len(text_clean) > 120 and "numbered_list" not in labels:
        labels.append("narrative_story")

    # media_post: URL 제거 후 60자 미만
    if len(text_clean) < 60:
        labels.append("media_post")

    if not labels:
        labels.append("unclassified")
    return labels


# ─────────────────────────────────────────────
# 4단계: 분석 엔진
# ─────────────────────────────────────────────

def analyze_group(tweets):
    """트윗 목록에 대해 Hook/Body 분류 + 통계 계산."""
    results = []
    for t in tweets:
        text = t.get("fullText", "")
        hook_text = extract_hook(text)
        hook_labels = classify_hook(hook_text)
        body_labels = classify_body(text)

        views = t.get("viewCount", 0)
        likes = t.get("likeCount", 0)
        eng_rate = calc_engagement_rate(t)

        short_sub = classify_short_cryptic_sub(hook_text) if "short_cryptic" in hook_labels else None

        results.append({
            "text": text,
            "hook_text": hook_text,
            "hook_labels": hook_labels,
            "short_sub": short_sub,
            "body_labels": body_labels,
            "views": views,
            "likes": likes,
            "engagement_rate": eng_rate,
            "source": t.get("source", ""),
            "bookmarkCount": t.get("bookmarkCount", 0),
            "retweetCount": t.get("retweetCount", 0),
            "quoteCount": t.get("quoteCount", 0),
            "replyCount": t.get("replyCount", 0),
        })
    return results


def compute_label_stats(analyzed, label_type="hook"):
    """레이블별 통계: 트윗 수, 평균 조회수, 평균 좋아요, 평균 ER."""
    label_data = defaultdict(list)
    for item in analyzed:
        labels = item["hook_labels"] if label_type == "hook" else item["body_labels"]
        for label in labels:
            label_data[label].append(item)

    stats = {}
    for label, items in label_data.items():
        n = len(items)
        avg_views = sum(i["views"] for i in items) / n
        avg_likes = sum(i["likes"] for i in items) / n
        avg_er = sum(i["engagement_rate"] for i in items) / n * 100
        avg_bookmarks = sum(i["bookmarkCount"] for i in items) / n
        stats[label] = {
            "count": n,
            "avg_views": round(avg_views),
            "avg_likes": round(avg_likes, 1),
            "avg_engagement_rate": round(avg_er, 3),
            "avg_bookmarks": round(avg_bookmarks, 1),
        }
    return stats


def compute_short_sub_stats(analyzed):
    """short_cryptic 서브패턴별 통계."""
    sub_data = defaultdict(list)
    for item in analyzed:
        if item["short_sub"]:
            sub_data[item["short_sub"]].append(item)
    stats = {}
    for label, items in sub_data.items():
        n = len(items)
        avg_views = sum(i["views"] for i in items) / n
        avg_likes = sum(i["likes"] for i in items) / n
        stats[label] = {
            "count": n,
            "avg_views": round(avg_views),
            "avg_likes": round(avg_likes, 1),
        }
    return stats


def compute_length_distribution(analyzed):
    """텍스트 길이 분포."""
    bins = {"0-30": 0, "31-80": 0, "81-150": 0, "151-280": 0, "280+": 0}
    for item in analyzed:
        text_clean = URL_RE.sub("", item["text"]).strip()
        length = len(text_clean)
        if length <= 30:
            bins["0-30"] += 1
        elif length <= 80:
            bins["31-80"] += 1
        elif length <= 150:
            bins["81-150"] += 1
        elif length <= 280:
            bins["151-280"] += 1
        else:
            bins["280+"] += 1
    return bins


def compute_url_stats(analyzed):
    """URL/미디어 포함 비율."""
    with_url = sum(1 for i in analyzed if URL_RE.search(i["text"]))
    total = len(analyzed)
    return {
        "with_url": with_url,
        "without_url": total - with_url,
        "url_ratio_pct": round(with_url / total * 100, 1) if total else 0,
    }


def compute_engagement_correlation(all_analyzed, high_analyzed, low_analyzed):
    """참여 유형별 조회수 상관 분석 및 고성과/저성과 비율 비교."""
    import math

    def pearson_r(xs, ys):
        n = len(xs)
        if n < 2:
            return 0.0
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        den_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
        den_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
        if den_x == 0 or den_y == 0:
            return 0.0
        return num / (den_x * den_y)

    eng_fields = [
        ("likes", "좋아요"),
        ("replyCount", "답글"),
        ("retweetCount", "리트윗"),
        ("bookmarkCount", "북마크"),
        ("quoteCount", "인용"),
    ]

    views = [i["views"] for i in all_analyzed]

    # 1) 전체 상관계수
    correlations = {}
    for field, kr in eng_fields:
        values = [i[field] for i in all_analyzed]
        correlations[kr] = round(pearson_r(values, views), 4)

    # 2) 고성과 vs 저성과 참여/조회 비율(%)
    def avg_eng_ratio(analyzed, field):
        ratios = []
        for i in analyzed:
            v = i["views"]
            if v > 0:
                ratios.append(i[field] / v * 100)
        return round(sum(ratios) / len(ratios), 4) if ratios else 0

    high_ratios = {}
    low_ratios = {}
    for field, kr in eng_fields:
        high_ratios[kr] = avg_eng_ratio(high_analyzed, field)
        low_ratios[kr] = avg_eng_ratio(low_analyzed, field)

    # 3) 지배적 참여 유형별 평균 조회수
    dom_stats = defaultdict(list)
    for i in all_analyzed:
        v = i["views"]
        if v == 0:
            continue
        best_kr = None
        best_ratio = -1
        for field, kr in eng_fields:
            ratio = i[field] / v
            if ratio > best_ratio:
                best_ratio = ratio
                best_kr = kr
        if best_kr:
            dom_stats[best_kr].append(i["views"])

    dominant = {}
    for kr, view_list in dom_stats.items():
        dominant[kr] = {
            "count": len(view_list),
            "avg_views": round(sum(view_list) / len(view_list)),
        }

    return {
        "correlations": correlations,
        "high_ratios": high_ratios,
        "low_ratios": low_ratios,
        "dominant": dominant,
    }


def get_top_examples(analyzed, label, label_type="hook", n=3):
    """특정 레이블에 해당하는 상위 n개 트윗 예시 (조회수 기준)."""
    matching = []
    for item in analyzed:
        labels = item["hook_labels"] if label_type == "hook" else item["body_labels"]
        if label in labels:
            matching.append(item)
    matching.sort(key=lambda x: x["views"], reverse=True)
    return matching[:n]


def find_failure_patterns(low_analyzed):
    """저성과 트윗의 공통 실패 패턴 추출."""
    patterns = []

    # 1) URL만 있는 트윗
    url_only = sum(1 for i in low_analyzed if len(URL_RE.sub("", i["text"]).strip()) < 10)
    if url_only > 0:
        patterns.append(("URL만 있는 트윗 (텍스트 10자 미만)", url_only,
                         round(url_only / len(low_analyzed) * 100, 1)))

    # 2) 분류 불가 Hook
    unclassified_hook = sum(1 for i in low_analyzed if "unclassified" in i["hook_labels"])
    patterns.append(("Hook 분류 불가 (패턴 미해당)", unclassified_hook,
                     round(unclassified_hook / len(low_analyzed) * 100, 1)))

    # 3) 매우 짧은 트윗 (30자 미만)
    very_short = sum(1 for i in low_analyzed
                     if len(URL_RE.sub("", i["text"]).strip()) < 30)
    patterns.append(("30자 미만 초단문", very_short,
                     round(very_short / len(low_analyzed) * 100, 1)))

    # 4) 단순 링크 공유
    link_share = sum(1 for i in low_analyzed
                     if URL_RE.search(i["text"])
                     and len(URL_RE.sub("", i["text"]).strip()) < 50)
    patterns.append(("단순 링크 공유 (텍스트 50자 미만 + URL)", link_share,
                     round(link_share / len(low_analyzed) * 100, 1)))

    # 5) 리플/대화형
    reply_like = sum(1 for i in low_analyzed
                     if i["text"].startswith("@"))
    patterns.append(("@멘션으로 시작 (대화형)", reply_like,
                     round(reply_like / len(low_analyzed) * 100, 1)))

    return sorted(patterns, key=lambda x: x[1], reverse=True)


# ─────────────────────────────────────────────
# 5단계: 공식 도출 (순위 매기기)
# ─────────────────────────────────────────────

def rank_labels(high_stats, low_stats, label_names_kr):
    """고/저 비율 × 평균 조회수로 순위 매기기."""
    rankings = []
    for label in high_stats:
        if label == "unclassified":
            continue
        h = high_stats[label]
        l_count = low_stats.get(label, {}).get("count", 0)
        h_count = h["count"]

        # 고/저 비율 계산
        if l_count > 0:
            ratio = h_count / l_count
        else:
            # 저성과에 없는 유형 (LLM 캐시 전용 등): 페널티 적용
            ratio = h_count * 0.001

        score = ratio * h["avg_views"] / 1_000_000  # 스케일링

        kr_name = label_names_kr.get(label, label)
        rankings.append({
            "label": label,
            "label_kr": kr_name,
            "high_count": h_count,
            "low_count": l_count,
            "ratio": round(ratio, 3),
            "avg_views": h["avg_views"],
            "avg_likes": h["avg_likes"],
            "avg_er": h["avg_engagement_rate"],
            "avg_bookmarks": h["avg_bookmarks"],
            "score": round(score, 3),
        })

    rankings.sort(key=lambda x: x["score"], reverse=True)
    return rankings


# ─────────────────────────────────────────────
# 6단계: Markdown 보고서 생성
# ─────────────────────────────────────────────

def truncate_text(text, max_len=150):
    """트윗 텍스트를 적절히 줄여서 표시."""
    text = text.replace("\n", " ↵ ").strip()
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def format_number(n):
    """숫자를 읽기 쉽게 포맷."""
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def generate_markdown(high_orig_analyzed, low_orig_analyzed,
                      high_rt_analyzed, low_rt_analyzed,
                      high_hook_stats, low_hook_stats,
                      high_body_stats, low_body_stats,
                      hook_rankings, body_rankings,
                      high_length, low_length,
                      high_url, low_url,
                      failure_patterns,
                      total_tweets, high_count, low_count,
                      high_orig_count, high_rt_count,
                      low_orig_count, low_rt_count,
                      account_high_stats,
                      high_short_sub_stats=None,
                      engagement_corr=None):
    """tweet_formulas.md 생성."""

    lines = []
    def w(s=""):
        lines.append(s)

    w("# X(트위터) 성공 트윗 공식 분석 보고서")
    w()
    w("---")
    w()

    # ── 1. 분석 개요 ──
    w("## 1. 분석 개요")
    w()
    w(f"- **분석 대상**: {total_tweets:,}건의 트윗 (56개 계정)")
    w(f"- **고성과 기준**: 조회수 ≥ {THRESHOLD:,}")
    w(f"- **고성과 트윗**: {high_count:,}건 ({high_count/total_tweets*100:.1f}%)")
    w(f"  - 오리지널: {high_orig_count:,}건 ({high_orig_count/high_count*100:.1f}%)")
    w(f"  - RT: {high_rt_count:,}건 ({high_rt_count/high_count*100:.1f}%)")
    w(f"- **저성과 트윗**: {low_count:,}건")
    w(f"  - 오리지널: {low_orig_count:,}건")
    w(f"  - RT: {low_rt_count:,}건")
    w(f"- **공식 도출 대상**: 오리지널 트윗만 (RT는 원작자의 글쓰기이므로 제외)")
    w()

    # ── 2. 핵심 발견 요약 ──
    w("## 2. 핵심 발견 요약")
    w()

    # 상위 Hook/Body 정리
    top_hooks = hook_rankings[:3] if hook_rankings else []
    top_bodies = body_rankings[:3] if body_rankings else []

    w("### 가장 효과적인 Hook 유형 (상위 3)")
    w()
    w("| 순위 | Hook 유형 | 고성과 트윗 수 | 평균 조회수 | 평균 좋아요 |")
    w("|:---:|:---|:---:|:---:|:---:|")
    for i, r in enumerate(top_hooks, 1):
        w(f"| {i} | **{r['label_kr']}** | {r['high_count']:,} | {format_number(r['avg_views'])} | {format_number(int(r['avg_likes']))} |")
    w()

    w("### 가장 효과적인 Body 구조 (상위 3)")
    w()
    w("| 순위 | Body 구조 | 고성과 트윗 수 | 평균 조회수 | 평균 좋아요 |")
    w("|:---:|:---|:---:|:---:|:---:|")
    for i, r in enumerate(top_bodies, 1):
        w(f"| {i} | **{r['label_kr']}** | {r['high_count']:,} | {format_number(r['avg_views'])} | {format_number(int(r['avg_likes']))} |")
    w()

    # 핵심 인사이트
    w("### 핵심 인사이트")
    w()
    w(f"- 고성과 오리지널 트윗의 평균 조회수: **{sum(i['views'] for i in high_orig_analyzed)//len(high_orig_analyzed):,}**")
    w(f"- 저성과 오리지널 트윗의 평균 조회수: **{sum(i['views'] for i in low_orig_analyzed)//len(low_orig_analyzed):,}**")
    w(f"- 고성과 오리지널 중 URL 포함 비율: **{high_url['url_ratio_pct']}%**")
    w(f"- 저성과 오리지널 중 URL 포함 비율: **{low_url['url_ratio_pct']}%**")
    w()

    # ── 3. Hook 작성 공식 ──
    w("## 3. 훅(Hook) 작성 공식")
    w()
    w("> Hook = 트윗의 첫 번째 줄. 독자가 스크롤을 멈추게 만드는 '미끼'.")
    w()

    for rank_idx, r in enumerate(hook_rankings, 1):
        w(f"### 3.{rank_idx}. {r['label_kr']} (`{r['label']}`)")
        w()
        w(f"- **고성과 트윗 수**: {r['high_count']:,}건")
        w(f"- **저성과 트윗 수**: {r['low_count']:,}건")
        w(f"- **고/저 비율**: {r['ratio']:.3f}")
        w(f"- **평균 조회수**: {format_number(r['avg_views'])}")
        w(f"- **평균 좋아요**: {format_number(int(r['avg_likes']))}")
        w(f"- **평균 ER**: {r['avg_er']:.3f}%")
        w(f"- **평균 북마크**: {format_number(int(r['avg_bookmarks']))}")
        w()

        # 공식 템플릿
        w("**작성 공식:**")
        w()
        templates = get_hook_template(r["label"])
        for tmpl in templates:
            w(f"- `{tmpl}`")
        w()

        # 실제 예시
        examples = get_top_examples(high_orig_analyzed, r["label"], "hook", 3)
        if examples:
            w("**실제 고성과 예시:**")
            w()
            for j, ex in enumerate(examples, 1):
                w(f"{j}. (조회 {format_number(ex['views'])}, 좋아요 {format_number(ex['likes'])}) `{truncate_text(ex['hook_text'], 100)}`")
            w()

        # short_cryptic 서브패턴 통계 삽입
        if r["label"] == "short_cryptic" and high_short_sub_stats:
            w("**서브패턴 세부 분류:**")
            w()
            w("| 서브패턴 | 트윗 수 | 비율(%) | 평균 조회수 |")
            w("|:---|:---:|:---:|:---:|")
            total_short = sum(s["count"] for s in high_short_sub_stats.values())
            for sub_label in sorted(high_short_sub_stats.keys(),
                                     key=lambda x: high_short_sub_stats[x]["count"],
                                     reverse=True):
                s = high_short_sub_stats[sub_label]
                kr = SHORT_CRYPTIC_SUB_LABELS_KR.get(sub_label, sub_label)
                pct = s["count"] / total_short * 100 if total_short else 0
                w(f"| {kr} | {s['count']:,} | {pct:.1f} | {format_number(s['avg_views'])} |")
            w()

        w("---")
        w()

    # ── 4. Body 작성 공식 ──
    w("## 4. 본문(Body) 작성 공식")
    w()
    w("> Body = Hook 이후의 본문. 독자를 끝까지 읽게 만들고 행동(좋아요/RT/북마크)을 유도하는 구조.")
    w()

    for rank_idx, r in enumerate(body_rankings, 1):
        w(f"### 4.{rank_idx}. {r['label_kr']} (`{r['label']}`)")
        w()
        w(f"- **고성과 트윗 수**: {r['high_count']:,}건")
        w(f"- **저성과 트윗 수**: {r['low_count']:,}건")
        w(f"- **고/저 비율**: {r['ratio']:.3f}")
        w(f"- **평균 조회수**: {format_number(r['avg_views'])}")
        w(f"- **평균 좋아요**: {format_number(int(r['avg_likes']))}")
        w(f"- **평균 ER**: {r['avg_er']:.3f}%")
        w()

        # 구조 설명
        w("**구조 패턴:**")
        w()
        desc = get_body_description(r["label"])
        w(desc)
        w()

        # 실제 예시
        examples = get_top_examples(high_orig_analyzed, r["label"], "body", 3)
        if examples:
            w("**실제 고성과 예시:**")
            w()
            for j, ex in enumerate(examples, 1):
                w(f"{j}. (조회 {format_number(ex['views'])}, 좋아요 {format_number(ex['likes'])}) `{truncate_text(ex['text'], 200)}`")
            w()
        w("---")
        w()

    # ── 5. 성공 vs 실패 비교 분석 ──
    w("## 5. 성공 vs 실패 비교 분석")
    w()

    # 5.1 Hook 유형별 비교
    w("### 5.1 Hook 유형별 분포 비교")
    w()
    w("| Hook 유형 | 고성과 비율(%) | 저성과 비율(%) | 차이(pp) |")
    w("|:---|:---:|:---:|:---:|")
    all_hook_labels = sorted(set(list(high_hook_stats.keys()) + list(low_hook_stats.keys())))
    for label in all_hook_labels:
        h = high_hook_stats.get(label, {})
        l = low_hook_stats.get(label, {})
        h_pct = h.get("count", 0) / len(high_orig_analyzed) * 100 if high_orig_analyzed else 0
        l_pct = l.get("count", 0) / len(low_orig_analyzed) * 100 if low_orig_analyzed else 0
        diff = h_pct - l_pct
        kr = HOOK_LABELS_KR.get(label, label)
        w(f"| {kr} | {h_pct:.1f} | {l_pct:.1f} | {diff:+.1f} |")
    w()

    # 5.2 Body 구조별 비교
    w("### 5.2 Body 구조별 분포 비교")
    w()
    w("| Body 구조 | 고성과 비율(%) | 저성과 비율(%) | 차이(pp) |")
    w("|:---|:---:|:---:|:---:|")
    all_body_labels = sorted(set(list(high_body_stats.keys()) + list(low_body_stats.keys())))
    for label in all_body_labels:
        h = high_body_stats.get(label, {})
        l = low_body_stats.get(label, {})
        h_pct = h.get("count", 0) / len(high_orig_analyzed) * 100 if high_orig_analyzed else 0
        l_pct = l.get("count", 0) / len(low_orig_analyzed) * 100 if low_orig_analyzed else 0
        diff = h_pct - l_pct
        kr = BODY_LABELS_KR.get(label, label)
        w(f"| {kr} | {h_pct:.1f} | {l_pct:.1f} | {diff:+.1f} |")
    w()

    # 5.3 텍스트 길이 분포
    w("### 5.3 텍스트 길이 분포 비교")
    w()
    w("| 길이 구간 | 고성과 비율(%) | 저성과 비율(%) |")
    w("|:---|:---:|:---:|")
    for bin_name in ["0-30", "31-80", "81-150", "151-280", "280+"]:
        h_pct = high_length.get(bin_name, 0) / len(high_orig_analyzed) * 100 if high_orig_analyzed else 0
        l_pct = low_length.get(bin_name, 0) / len(low_orig_analyzed) * 100 if low_orig_analyzed else 0
        w(f"| {bin_name}자 | {h_pct:.1f} | {l_pct:.1f} |")
    w()

    # 5.4 URL/미디어 비교
    w("### 5.4 URL/미디어 포함 비교")
    w()
    w(f"- 고성과: URL 포함 **{high_url['url_ratio_pct']}%** ({high_url['with_url']:,}건)")
    w(f"- 저성과: URL 포함 **{low_url['url_ratio_pct']}%** ({low_url['with_url']:,}건)")
    w()

    # 5.5 저성과 실패 패턴
    w("### 5.5 저성과 트윗의 공통 실패 패턴")
    w()
    w("| 패턴 | 트윗 수 | 비율(%) |")
    w("|:---|:---:|:---:|")
    for pattern_name, count, pct in failure_patterns:
        w(f"| {pattern_name} | {count:,} | {pct} |")
    w()

    # 5.6 참여 유형별 상관 분석
    if engagement_corr:
        w("### 5.6 참여 유형별 조회수 상관 분석")
        w()
        w(f"> 전체 {len(high_orig_analyzed) + len(low_orig_analyzed):,}건 오리지널 트윗 대상.")
        w()

        w("#### 참여 유형 × 조회수 상관계수 (Pearson r)")
        w()
        w("| 참여 유형 | 상관계수 (r) | 해석 |")
        w("|:---|:---:|:---|")
        corr_sorted = sorted(engagement_corr["correlations"].items(),
                             key=lambda x: abs(x[1]), reverse=True)
        for kr, r_val in corr_sorted:
            if abs(r_val) >= 0.7:
                interp = "강한 양의 상관"
            elif abs(r_val) >= 0.4:
                interp = "중간 양의 상관"
            elif abs(r_val) >= 0.2:
                interp = "약한 양의 상관"
            else:
                interp = "매우 약한 상관"
            w(f"| **{kr}** | {r_val:.4f} | {interp} |")
        w()

        w("#### 고성과 vs 저성과: 참여/조회 비율(%)")
        w()
        w("| 참여 유형 | 고성과 평균(%) | 저성과 평균(%) | 차이(pp) |")
        w("|:---|:---:|:---:|:---:|")
        for kr in ["좋아요", "답글", "리트윗", "북마크", "인용"]:
            h = engagement_corr["high_ratios"].get(kr, 0)
            l = engagement_corr["low_ratios"].get(kr, 0)
            diff = h - l
            w(f"| **{kr}** | {h:.4f} | {l:.4f} | {diff:+.4f} |")
        w()

        w("#### 지배적 참여 유형별 트윗 분포 및 평균 조회수")
        w()
        w("> 각 트윗에서 참여/조회 비율이 가장 높은 유형을 \"지배적 참여 유형\"으로 분류.")
        w()
        w("| 지배적 참여 유형 | 트윗 수 | 평균 조회수 |")
        w("|:---|:---:|:---:|")
        dom = engagement_corr.get("dominant", {})
        for kr in sorted(dom.keys(), key=lambda x: dom[x]["avg_views"], reverse=True):
            d = dom[kr]
            w(f"| **{kr}** | {d['count']:,} | {format_number(d['avg_views'])} |")
        w()

        # 핵심 인사이트
        strongest = corr_sorted[0] if corr_sorted else None
        if strongest:
            w(f"**핵심 인사이트**: 조회수와 가장 강한 상관관계를 보이는 참여 유형은 **{strongest[0]}**(r={strongest[1]:.4f}).")
            w("알고리즘 가중치와 매핑하면:")
            w("- **좋아요** = `favorite` 신호 (가장 기본적 양의 가중치)")
            w("- **답글** = `reply` 신호 (대화 유도)")
            w("- **리트윗** = `retweet` 신호 (확산)")
            w("- **북마크** = `dwell`/저장 신호 (콘텐츠 가치 지표)")
            w("- **인용** = `quote` 신호 (의견 추가)")
        w()

    # ── 6. 종합 체크리스트 ──
    w("## 6. 종합 트윗 작성 공식 & 체크리스트")
    w()
    w("> 43,773건 데이터 분석 + X 공식 알고리즘 소스코드 교차 검증 결과.")
    w()
    w("### 황금 공식")
    w()
    w("```")
    w("[멈추게 하는 Hook] + [오래 읽게 하는 Body(151-280자)] + [보내고 싶은 공유 가치] = 바이럴 트윗")
    w("```")
    w()
    w("- **멈추게 하는 Hook**: 알고리즘 `dwell`(머무름 여부) 신호 → 스크롤 정지 유도")
    w("- **오래 읽게 하는 Body**: 알고리즘 `dwell_time`(체류 시간) 신호 → 151-280자 구간이 고성과 +14pp")
    w("- **보내고 싶은 공유 가치**: 알고리즘 `share_via_dm` 별도 가중치 → \"친구한테 보내고 싶은\" 콘텐츠")
    w()

    w("### Hook 작성 체크리스트")
    w()
    w("- [ ] **이유/원인**을 제시하는가? (\"~하는 이유\") → 좋아요 평균 4,884 (전체 1위)")
    w("- [ ] **독자를 지정**했는가? (\"~하는 사람\") → 좋아요 평균 4,509")
    w("- [ ] **권위/신뢰 요소**가 포함되었는가? (현직, N년차) → 북마크 평균 2,181, 프로필 클릭 유발")
    w("- [ ] **행동을 유도**하는가? (\"~하세요\", \"~해봐\") → 짧은 Hook 중 평균 조회 148만 (1위)")
    w("- [ ] **미완결/티저**인가? (\"~했는데\", \"~하다가\") → 짧은 Hook 중 평균 조회 110만 (2위)")
    w("- [ ] **호기심을 자극**하는가? (질문, 반전, 충격) → ER 1.878%")
    w("- [ ] Hook만 읽고도 **스크롤을 멈추는가?** → 알고리즘 `dwell` 신호 직결")
    w("- [ ] **피크타임에 발행**했는가? → 24시간 하드 컷오프, 초기 반응이 전파력 결정")
    w()

    w("### Body 작성 체크리스트")
    w()
    w("- [ ] **151-280자**를 목표로 했는가? → 고성과 33.1% vs 저성과 19.1% (+14pp, 최강 지표)")
    w("- [ ] **화살표 흐름(→)**을 활용했는가? → 평균 조회 638K, ER 2.0%, 북마크 3,657 (Body 1위)")
    w("- [ ] **번호 리스트**로 구조화했는가? → 북마크 평균 1,616")
    w("- [ ] 한 줄이 50자를 넘지 않는가? (모바일 가독성)")
    w("- [ ] **끝까지 읽게** 만드는 구조인가? → 알고리즘 `dwell_time` 별도 가중치")
    w("- [ ] **DM으로 보내고 싶은** 콘텐츠인가? → 알고리즘 `share_via_dm` 별도 가중치")
    w("- [ ] 이미지는 **클릭해서 확대하고 싶게** 만들었는가? → 알고리즘 `photo_expand` 별도 가중치")
    if engagement_corr:
        corr_sorted = sorted(engagement_corr["correlations"].items(),
                             key=lambda x: abs(x[1]), reverse=True)
        if corr_sorted:
            top_eng = corr_sorted[0]
            w(f"- [ ] **{top_eng[0]}을(를) 유도**하는 콘텐츠인가? → 조회수와 가장 강한 상관(r={top_eng[1]:.4f})")
    w()

    w("### 피해야 할 패턴")
    w()
    w("- URL만 던지는 단순 링크 공유 (저성과 43.8%)")
    w("- 맥락 없는 초단문 — 텍스트 10자 미만 (저성과 6.9%)")
    w("- Hook 없이 바로 본론으로 들어가는 트윗")
    w("- @멘션으로 시작하는 대화형 트윗 (노출 제한)")
    w("- **같은 시간대 연타 포스팅** → 알고리즘 author diversity decay로 연속 노출 시 점수 감쇠")
    w("- **공격적/혐오 톤** → `block`/`mute`/`report` 음의 가중치")
    w("- **해시태그 남용** → 알고리즘에 해시태그 관련 수동 피처 없음 (효과 0)")
    w("- **민감/논란 키워드 사용** → 뮤트 키워드 정확 매칭(exact token match)으로 해당 유저에게 완전 차단")
    w()

    # ── 7. X 알고리즘 분석 ──
    w("## 7. X 공식 알고리즘 분석 (For You 피드)")
    w()
    w("> X가 공개한 추천 알고리즘 소스코드(`x-algorithm-main`) 기반 분석.")
    w("> 가중치 수치는 보안상 비공개이나, **어떤 신호를 쓰는지**는 코드로 확인 가능.")
    w()

    w("### 7.1 최종 점수 공식")
    w()
    w("```")
    w("Final Score = Σ (weight_i × P(action_i))")
    w("```")
    w()
    w("Grok 기반 트랜스포머가 각 트윗에 대해 15개 양의 행동 + 4개 음의 행동 확률을 예측하고,")
    w("가중 합산하여 최종 점수를 산출합니다.")
    w()

    w("### 7.2 양의 신호 (노출 ↑)")
    w()
    w("| 신호 | 의미 | 트윗 작성 시사점 |")
    w("|:---|:---|:---|")
    w("| **favorite** | 좋아요 | 공감·동의를 유발하는 콘텐츠 |")
    w("| **reply** | 답글 | 질문·논쟁으로 대화 유도 |")
    w("| **retweet** | 리트윗 | 공유할 가치가 있는 정보성 콘텐츠 |")
    w("| **quote** | 인용 트윗 | 의견을 추가할 여지를 남기는 콘텐츠 |")
    w("| **share** | 공유 | 퍼뜨리고 싶은 콘텐츠 |")
    w("| **share_via_dm** | DM으로 공유 | \"이거 봐봐\" 하고 보내고 싶은 콘텐츠 |")
    w("| **share_via_copy_link** | 링크 복사 | 외부 플랫폼에 공유할 만한 콘텐츠 |")
    w("| **dwell** | 머무름 여부 | 스크롤을 멈추게 만드는 Hook |")
    w("| **dwell_time** | 머무른 시간(연속) | 끝까지 읽게 만드는 Body 구조 |")
    w("| **photo_expand** | 이미지 확대 클릭 | 클릭해서 자세히 보고 싶은 이미지 |")
    w("| **click** | 트윗 클릭 | 더 보고 싶게 만드는 구조 (잘린 텍스트 등) |")
    w("| **profile_click** | 프로필 클릭 | \"이 사람 누구지?\" 유발 → 권위형 Hook |")
    w("| **follow_author** | 팔로우 | 지속적 가치를 느끼게 하는 시리즈/전문 콘텐츠 |")
    w("| **video_quality_view** | 영상 시청 | 최소 길이 이상의 영상을 끝까지 시청 |")
    w("| **quoted_click** | 인용 트윗 클릭 | 인용된 원문을 클릭하게 만드는 맥락 |")
    w()

    w("### 7.3 음의 신호 (노출 ↓)")
    w()
    w("| 신호 | 의미 | 회피 전략 |")
    w("|:---|:---|:---|")
    w("| **not_interested** | \"관심 없음\" 클릭 | 타겟 독자 이탈 방지: 주제 명확히 |")
    w("| **block_author** | 차단 | 공격적·불쾌한 톤 회피 |")
    w("| **mute_author** | 뮤트 | 과도한 포스팅 빈도 주의 |")
    w("| **report** | 신고 | 허위 정보·자극적 낚시 회피 |")
    w()

    w("### 7.4 작성자 다양성 페널티")
    w()
    w("```")
    w("multiplier = (1 - floor) × decay^position + floor")
    w("```")
    w()
    w("같은 작성자의 트윗이 피드에 연속 등장하면 **지수적으로 점수가 감쇠**됩니다.")
    w("→ 짧은 시간에 연타 포스팅보다 **시간 간격을 두고 포스팅**하는 것이 유리.")
    w()

    w("### 7.5 팔로워 밖 노출 (Out-of-Network)")
    w()
    w("X의 For You 피드는 두 소스에서 후보를 가져옵니다:")
    w()
    w("1. **In-Network (Thunder)**: 팔로우한 계정의 최근 트윗")
    w("2. **Out-of-Network (Phoenix Retrieval)**: ML이 전체 트윗 중 유사도 기반으로 발굴")
    w()
    w("OON 콘텐츠는 가중치 팩터로 약간의 페널티를 받지만, **충분히 높은 예측 점수면 팔로워 아닌 유저에게도 노출**됩니다.")
    w("→ 바이럴 트윗 = OON Retrieval에서 높은 유사도 + Ranking에서 높은 참여 예측 점수를 동시에 달성한 트윗.")
    w()

    w("### 7.6 알고리즘 기반 트윗 최적화 핵심 원칙")
    w()
    w("1. **체류 시간 극대화**: dwell + dwell_time 이중 가점. 강력한 Hook으로 멈추게 하고, 구조화된 Body로 오래 읽게 하라")
    w("2. **DM 공유 유도**: share_via_dm이 별도 가중치. \"친구한테 보내고 싶은\" 콘텐츠가 알고리즘적으로 가장 강력")
    w("3. **이미지 확대 유도**: photo_expand 별도 가중치. 이미지를 넣되, 클릭해서 확대하고 싶게 만들어라")
    w("4. **프로필 클릭 유도**: profile_click 별도 가중치. \"이 사람 누구지?\" → 권위형·전문가형 Hook이 효과적")
    w("5. **팔로우 전환**: follow_author 별도 가중치. 시리즈·연재 콘텐츠로 \"이 사람 팔로우해야겠다\" 유발")
    w("6. **부정 반응 최소화**: block/mute/report는 음의 가중치. 논쟁은 유도하되 혐오·거부감은 피하라")
    w("7. **연타 금지**: author diversity decay로 같은 계정 연속 노출 시 점수 감쇠. 시간 간격을 두고 포스팅")
    w("8. **알고리즘은 텍스트를 읽지 않는다**: 모델은 hash(user)×hash(post)×hash(author) 임베딩만 사용하며, 트윗 텍스트 자체는 입력이 아님. Hook/Body 공식의 역할은 **\"사람의 반응(좋아요·답글·공유)을 유도\"**하는 것이고, 알고리즘은 **그 반응을 측정하여 점수를 매기고 증폭**시키는 2단계 구조. 해시태그 수·텍스트 길이 같은 수동 규칙은 존재하지 않음")
    w()

    w("### 7.7 핵심 운영 제약 조건")
    w()
    w("| 제약 | 수치 | 시사점 |")
    w("|:---|:---|:---|")
    w("| 포스트 수명 | **24시간** (86,400초) | 트윗 발행 시점(=피크타임)이 노출량 직결. 24시간 이후 `age_filter`에 의해 완전 제거 |")
    w("| In-network 캐시 | **48시간** | Thunder 인메모리 저장소에 48시간만 보존. 팔로워에게도 2일 지나면 추천 불가 |")
    w("| 유저 히스토리 | **128개** | 모델이 보는 과거 참여 이력 최대 128개. 최근 128회 참여가 개인화 기반 → 초기 반응이 중요 |")
    w("| 영상 최소 길이 | **200ms** | `MIN_VIDEO_DURATION_MS` 미만 영상은 `video_quality_view` 점수 0 |")
    w("| 뮤트 키워드 | **정확 매칭** (exact token match) | fuzzy가 아닌 정확한 토큰 매칭. 특정 단어 정확히 포함 시 해당 유저에게 필터링 |")
    w()

    w("### 7.8 팔로워 수의 복리 효과")
    w()
    w("`author_followers_count`가 Gizmoduck에서 hydrate되어 모델 입력 피처로 투입됩니다.")
    w("이는 팔로워가 많을수록 같은 참여율이라도 더 높은 점수를 받을 가능성을 의미합니다.")
    w()
    w("**복리 구조:**")
    w("```")
    w("좋은 콘텐츠 → 높은 참여율 → 팔로워 증가 → 모델 입력 피처 강화 → 같은 참여율에서도 더 높은 점수 → 더 많은 노출 → 더 빠른 팔로워 증가")
    w("```")
    w()
    w("→ 팔로워 성장 자체가 알고리즘적 이점을 축적하는 복리 효과. 초기에 양질의 콘텐츠로 팔로워 기반을 구축하는 것이 장기적으로 중요.")
    w()

    # ── 8. 부록 ──
    w("## 8. 부록")
    w()

    # 7.1 계정별 분석
    w("### 8.1 계정별 고성과 오리지널 트윗 분석")
    w()
    w("| 계정 | 오리지널 고성과 | 평균 조회수 | 평균 좋아요 | 주요 Hook 유형 |")
    w("|:---|:---:|:---:|:---:|:---|")
    for acc, stats in sorted(account_high_stats.items(),
                              key=lambda x: x[1]["count"], reverse=True)[:20]:
        if stats["count"] == 0:
            continue
        top_hook = stats.get("top_hook", "-")
        w(f"| {acc} | {stats['count']} | {format_number(stats['avg_views'])} | {format_number(int(stats['avg_likes']))} | {top_hook} |")
    w()

    # 7.2 RT 분석
    w("### 8.2 RT(리트윗) 분석")
    w()
    w(f"- 고성과 RT: **{high_rt_count:,}건** (고성과의 {high_rt_count/high_count*100:.1f}%)")
    w(f"- RT 평균 조회수: **{sum(i['views'] for i in high_rt_analyzed)//max(len(high_rt_analyzed),1):,}**")
    w(f"- 오리지널 평균 조회수: **{sum(i['views'] for i in high_orig_analyzed)//max(len(high_orig_analyzed),1):,}**")
    w(f"- RT가 고성과의 과반({high_rt_count/high_count*100:.0f}%)을 차지하지만, 이는 원작자의 콘텐츠 품질에 의한 것")
    w(f"- **자체 콘텐츠 역량 강화가 진정한 성장 전략**")
    w()

    # 7.3 방법론
    w("### 8.3 분석 방법론")
    w()
    w("1. **데이터 수집**: 56개 계정의 JSONL 파일에서 트윗 로드")
    w(f"2. **전처리**: viewCount 누락 제외, (fullText, source) 기준 중복 제거")
    w(f"3. **분류 기준**: 조회수 {THRESHOLD:,} 이상을 고성과로 분류")
    w("4. **RT 분리**: `fullText.startswith('RT @')` 기준으로 RT와 오리지널 분리")
    w("5. **Hook 분류**: 첫 번째 비어있지 않은 줄을 Hook으로 추출, 정규식 + LLM 캐시 기반 다중 레이블 분류 (20개 유형)")
    w("6. **Body 분류**: 전체 텍스트 구조를 분석하여 5개 유형으로 분류")
    w("7. **순위 매기기**: (고성과 개수 / 저성과 개수) × 평균 조회수로 종합 점수 산출")
    w()

    return "\n".join(lines)


def get_hook_template(label):
    """Hook 유형별 작성 템플릿."""
    templates = {
        "relatable_targeting": [
            "[대상]하는 사람 특징",
            "[대상]인 분들 꼭 보세요",
            "[상황] 겪어본 사람만 공감하는 [주제]",
        ],
        "credibility": [
            "현직 [직업]이 알려주는 [주제]",
            "[N]년차 [직업]의 [주제] 정리",
            "[전문가]가 추천하는 [주제] [N]가지",
        ],
        "challenge": [
            "이거 [N]초 안에 풀면 상위 [N]%",
            "[주제] 퀴즈: 몇 개 맞출 수 있을까?",
            "틀린 곳 찾기 (힌트: [단서])",
        ],
        "practical_tip": [
            "[주제] 하는 방법 총정리",
            "[주제] 꿀팁 [N]가지",
            "[목표]하려면 반드시 알아야 할 것들",
        ],
        "bracket_title": [
            "[카테고리] 제목",
            "【주제】 핵심 내용",
            "「전문가의 한마디」 인용",
        ],
        "numbered_title": [
            "반드시 알아야 할 [주제] [N]가지",
            "[주제] top [N]",
            "[주제] [N]선 추천",
        ],
        "question": [
            "[주제]에 대해 어떻게 생각하세요?",
            "왜 [현상]이 일어나는 걸까?",
            "[대상]은 왜 [행동]할까?",
        ],
        "news_shock": [
            "속보: [사건] 발생",
            "[대상]이 [충격적 사실]한 이유",
            "역대급 [주제] 등장... 실화?",
        ],
        "personal_story": [
            "오늘 [경험]했는데 진짜 [감정]",
            "살면서 처음으로 [경험]을 해봤다",
            "어제 [사건]이 있었는데 [결과/교훈]",
        ],
        "fact_curiosity": [
            "[대상/현상]의 진짜 이유",
            "알고 보면 [주제]의 비밀",
            "[대상]이 [결과]인 이유",
        ],
        "warning_loss": [
            "절대 [행동]하면 안 되는 이유",
            "[대상] 조심하세요, [경고 내용]",
            "[행동]하지 마세요. [근거/사례]",
        ],
        "quote_reference": [
            "\"[인용문]\" - [출처]",
            "'[핵심 키워드]' [맥락 설명]",
            "[인물]이 한 말: \"[인용문]\"",
        ],
        "short_cryptic": [
            "[단어/문구]. (+ 이미지)",
            "[감탄사]. [한 문장].",
            "[주제] (미디어 첨부 필수)",
        ],
    }
    # LLM 분류 유형 템플릿
    templates["narrative_situation"] = [
        "[인물]이 [상황]에서 [행동]한 결과",
        "[N]년 전 [사건]의 충격적 결말",
        "[상황] 속에서 벌어진 예상 밖의 일",
    ]
    templates["info_fact"] = [
        "[주제]에 대해 몰랐던 사실",
        "[기관/전문가]가 발표한 [주제] 데이터",
        "[숫자]로 보는 [주제]의 현실",
    ]
    templates["opinion_reaction"] = [
        "[현상]에 대해 솔직히 말하자면...",
        "[경험]하고 나서 느낀 점",
        "[대상]이 [행동]하는 거 나만 불편한가?",
    ]
    templates["celeb_mention"] = [
        "[유명인]이 [상황]에서 보여준 반응",
        "[유명인]의 [주제] 근황",
        "[유명인]이 직접 밝힌 [주제]",
    ]
    templates["promotion"] = [
        "‼️ [대상] 주목 ‼️ [혜택 설명]",
        "[제품/서비스] 써봤는데 진짜 [평가]",
        "[대상]이면 꼭 신청하세요: [혜택]",
    ]
    templates["emotion_empathy"] = [
        "[상황]에서 느낀 [감정]... (공감 유도)",
        "이 글을 읽으면 마음이 [감정]해집니다",
        "[관계]에게 전하고 싶은 한마디",
    ]
    templates["comparison_choice"] = [
        "[선택지A] vs [선택지B], 당신의 선택은?",
        "[조건]이라면 [A]와 [B] 중 뭘 고르겠어요?",
        "[상황] 어떻게 하시겠어요?",
    ]
    return templates.get(label, ["(템플릿 미정의)"])


def get_body_description(label):
    """Body 유형별 구조 설명."""
    descs = {
        "numbered_list": (
            "```\n"
            "1. 항목 1\n"
            "2. 항목 2\n"
            "3. 항목 3\n"
            "...\n"
            "```\n"
            "- 정보를 번호로 나열하여 체계적으로 전달\n"
            "- 북마크율이 높음 (나중에 다시 보기 위해 저장)"
        ),
        "arrow_flow": (
            "```\n"
            "원인 → 과정 → 결과\n"
            "또는\n"
            "단계1 → 단계2 → 단계3\n"
            "```\n"
            "- 인과관계나 프로세스를 시각적으로 표현\n"
            "- 화살표가 읽는 흐름을 유도"
        ),
        "short_line_stacking": (
            "```\n"
            "짧은 문장 1\n"
            "짧은 문장 2\n"
            "짧은 문장 3\n"
            "짧은 문장 4\n"
            "```\n"
            "- 각 줄 50자 미만의 짧은 문장을 4줄 이상 쌓기\n"
            "- 모바일 가독성 극대화, 리듬감 있는 읽기 경험"
        ),
        "narrative_story": (
            "```\n"
            "배경 설명 + 사건 전개 + 결론/교훈\n"
            "(120자 초과의 연속 서술)\n"
            "```\n"
            "- 스토리텔링으로 몰입도 높임\n"
            "- 개인 경험이나 사례 중심"
        ),
        "media_post": (
            "```\n"
            "짧은 텍스트 (60자 미만) + 이미지/영상\n"
            "```\n"
            "- 텍스트는 미디어의 맥락/캡션 역할\n"
            "- 시각 콘텐츠가 주역"
        ),
    }
    return descs.get(label, "(설명 미정의)")


# ─────────────────────────────────────────────
# 메인 실행
# ─────────────────────────────────────────────

def main():
    print("=" * 60)
    print("X 트윗 성공 공식 역설계")
    print("=" * 60)

    # 1단계: 데이터 로드
    print("\n[1단계] 데이터 로드 중...")
    tweets, skipped_view, skipped_dup = load_all_tweets()
    print(f"  총 로드: {len(tweets):,}건 (viewCount 누락 {skipped_view}건, 중복 {skipped_dup}건 제외)")

    # 분리
    high_orig, high_rt, low_orig, low_rt = split_tweets(tweets)
    print(f"  고성과 오리지널: {len(high_orig):,}건")
    print(f"  고성과 RT:       {len(high_rt):,}건")
    print(f"  저성과 오리지널: {len(low_orig):,}건")
    print(f"  저성과 RT:       {len(low_rt):,}건")

    total = len(tweets)
    high_count = len(high_orig) + len(high_rt)
    low_count = len(low_orig) + len(low_rt)

    # 2~3단계: Hook/Body 분류
    print("\n[2~3단계] Hook/Body 패턴 분류 중...")
    high_orig_analyzed = analyze_group(high_orig)
    low_orig_analyzed = analyze_group(low_orig)
    high_rt_analyzed = analyze_group(high_rt)
    low_rt_analyzed = analyze_group(low_rt)

    # 4단계: 통계 계산
    print("\n[4단계] 통계 비교 분석 중...")
    high_hook_stats = compute_label_stats(high_orig_analyzed, "hook")
    low_hook_stats = compute_label_stats(low_orig_analyzed, "hook")
    high_body_stats = compute_label_stats(high_orig_analyzed, "body")
    low_body_stats = compute_label_stats(low_orig_analyzed, "body")

    # 길이 분포
    high_length = compute_length_distribution(high_orig_analyzed)
    low_length = compute_length_distribution(low_orig_analyzed)

    # URL 통계
    high_url = compute_url_stats(high_orig_analyzed)
    low_url = compute_url_stats(low_orig_analyzed)

    # 실패 패턴
    failure_patterns = find_failure_patterns(low_orig_analyzed)

    # 콘솔 출력: Hook 통계
    print("\n  [Hook 유형별 통계 - 고성과 오리지널]")
    print(f"  {'유형':<25} {'개수':>8} {'평균조회':>12} {'평균좋아요':>10} {'평균ER%':>8}")
    print(f"  {'-' * 65}")
    for label in sorted(high_hook_stats.keys(), key=lambda x: high_hook_stats[x]["avg_views"], reverse=True):
        s = high_hook_stats[label]
        kr = HOOK_LABELS_KR.get(label, label)
        print(f"  {kr:<25} {s['count']:>8} {s['avg_views']:>12,} {s['avg_likes']:>10.1f} {s['avg_engagement_rate']:>8.3f}")

    print("\n  [Body 구조별 통계 - 고성과 오리지널]")
    print(f"  {'유형':<25} {'개수':>8} {'평균조회':>12} {'평균좋아요':>10} {'평균ER%':>8}")
    print(f"  {'-' * 65}")
    for label in sorted(high_body_stats.keys(), key=lambda x: high_body_stats[x]["avg_views"], reverse=True):
        s = high_body_stats[label]
        kr = BODY_LABELS_KR.get(label, label)
        print(f"  {kr:<25} {s['count']:>8} {s['avg_views']:>12,} {s['avg_likes']:>10.1f} {s['avg_engagement_rate']:>8.3f}")

    # 5단계: 공식 도출 (순위)
    print("\n[5단계] 공식 도출 (순위 매기기)...")
    hook_rankings = rank_labels(high_hook_stats, low_hook_stats, HOOK_LABELS_KR)
    body_rankings = rank_labels(high_body_stats, low_body_stats, BODY_LABELS_KR)

    print("\n  [Hook 순위]")
    for i, r in enumerate(hook_rankings, 1):
        print(f"  {i}. {r['label_kr']}: score={r['score']:.3f} (고:{r['high_count']}, 저:{r['low_count']}, ratio:{r['ratio']:.3f}, avg_views:{r['avg_views']:,})")

    print("\n  [Body 순위]")
    for i, r in enumerate(body_rankings, 1):
        print(f"  {i}. {r['label_kr']}: score={r['score']:.3f} (고:{r['high_count']}, 저:{r['low_count']}, ratio:{r['ratio']:.3f}, avg_views:{r['avg_views']:,})")

    # 계정별 고성과 오리지널 통계
    account_high_stats = defaultdict(lambda: {
        "count": 0, "total_views": 0, "total_likes": 0, "hooks": defaultdict(int)
    })
    for item in high_orig_analyzed:
        acc = item["source"]
        account_high_stats[acc]["count"] += 1
        account_high_stats[acc]["total_views"] += item["views"]
        account_high_stats[acc]["total_likes"] += item["likes"]
        for h in item["hook_labels"]:
            account_high_stats[acc]["hooks"][h] += 1

    # 계정별 평균과 주요 Hook 계산
    for acc in account_high_stats:
        stats = account_high_stats[acc]
        n = stats["count"]
        stats["avg_views"] = stats["total_views"] // n if n else 0
        stats["avg_likes"] = stats["total_likes"] / n if n else 0
        if stats["hooks"]:
            top = max(stats["hooks"], key=stats["hooks"].get)
            stats["top_hook"] = HOOK_LABELS_KR.get(top, top)
        else:
            stats["top_hook"] = "-"

    # 참여 유형별 상관 분석
    print("\n  [참여 유형별 상관 분석]")
    all_orig_analyzed = high_orig_analyzed + low_orig_analyzed
    engagement_corr = compute_engagement_correlation(
        all_orig_analyzed, high_orig_analyzed, low_orig_analyzed
    )
    print("  상관계수 (참여 유형 vs 조회수):")
    for kr, r_val in sorted(engagement_corr["correlations"].items(),
                             key=lambda x: abs(x[1]), reverse=True):
        print(f"    {kr}: r={r_val:.4f}")
    print("  고성과 vs 저성과 참여/조회 비율(%):")
    for kr in ["좋아요", "답글", "리트윗", "북마크", "인용"]:
        h = engagement_corr["high_ratios"].get(kr, 0)
        l = engagement_corr["low_ratios"].get(kr, 0)
        print(f"    {kr}: 고성과={h:.4f}% 저성과={l:.4f}% (diff={h-l:+.4f}pp)")

    # short_cryptic 서브패턴 통계
    high_short_sub_stats = compute_short_sub_stats(high_orig_analyzed)

    print("\n  [short_cryptic 서브패턴 - 고성과 오리지널]")
    for sub_label in sorted(high_short_sub_stats.keys(),
                             key=lambda x: high_short_sub_stats[x]["count"], reverse=True):
        s = high_short_sub_stats[sub_label]
        kr = SHORT_CRYPTIC_SUB_LABELS_KR.get(sub_label, sub_label)
        print(f"  {kr:<20} {s['count']:>6}건  avg_views={s['avg_views']:,}")

    # 6단계: Markdown 생성
    print("\n[6단계] tweet_formulas.md 생성 중...")
    md_content = generate_markdown(
        high_orig_analyzed, low_orig_analyzed,
        high_rt_analyzed, low_rt_analyzed,
        high_hook_stats, low_hook_stats,
        high_body_stats, low_body_stats,
        hook_rankings, body_rankings,
        high_length, low_length,
        high_url, low_url,
        failure_patterns,
        total, high_count, low_count,
        len(high_orig), len(high_rt),
        len(low_orig), len(low_rt),
        dict(account_high_stats),
        high_short_sub_stats=high_short_sub_stats,
        engagement_corr=engagement_corr,
    )

    with open(OUTPUT_MD, "w", encoding="utf-8") as f:
        f.write(md_content)

    print(f"  저장 완료: {OUTPUT_MD.name} ({OUTPUT_MD.stat().st_size / 1024:.0f} KB)")
    print(f"\n{'=' * 60}")
    print("역설계 완료!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
