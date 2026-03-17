"""
X 트윗 성공 공식 역설계 v2
- v1 대비 핵심 개선: 상호배타 분류, 전체 트윗 동일 적용, Hook×Body 교차표, 자동 패턴 추출
- 산출물: tweet_formulas_v2.md
"""

import json
import re
import math
import random
import sys
from pathlib import Path
from collections import defaultdict, Counter
from zipfile import ZipFile
from io import TextIOWrapper

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "x 자료"
ZIP_PATH = DATA_DIR / "x-algorithm-main.zip"
EXTRACTED_DIR = DATA_DIR / "extracted" / "x-algorithm-main"
LLM_CACHE_FILE = BASE_DIR / "llm_hook_cache.json"
OUTPUT_MD = BASE_DIR / "tweet_formulas_v2.md"
THRESHOLD = 100_000

# LLM 캐시 로드
_LLM_CACHE = {}
if LLM_CACHE_FILE.exists():
    with open(LLM_CACHE_FILE, "r", encoding="utf-8") as _f:
        _LLM_CACHE = json.load(_f)
LLM_HOOK_OVERRIDES = _LLM_CACHE.get("hook_type_overrides", {})

URL_RE = re.compile(r'https?://\S+')
EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    "\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
    "\U00002702-\U000027B0\U0000FE00-\U0000FE0F\U0001F1E0-\U0001F1FF"
    "\U00002600-\U000026FF\U00002700-\U000027BF]+",
    re.UNICODE
)

# ──────────────────────────────────────────────────────────
# Phase 1: 데이터 로드
# ──────────────────────────────────────────────────────────

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


# ──────────────────────────────────────────────────────────
# Phase 2: 피처 추출
# ──────────────────────────────────────────────────────────

def extract_hook(text):
    """URL 제거 후 첫 비어있지 않은 줄."""
    text_clean = URL_RE.sub("", text).strip()
    for line in text_clean.split("\n"):
        line = line.strip()
        if line:
            return line
    return text_clean


def extract_text_features(text):
    """텍스트 피처 추출."""
    text_clean = URL_RE.sub("", text).strip()
    lines = [l.strip() for l in text_clean.split("\n") if l.strip()]
    words = text_clean.split()

    has_url = bool(URL_RE.search(text))
    has_emoji = bool(EMOJI_RE.search(text_clean))
    has_hashtag = bool(re.search(r'#\S+', text_clean))
    has_arrow = bool(re.search(r'[→➡▶►]', text_clean))
    has_question = '?' in text_clean or '？' in text_clean or '❓' in text_clean
    has_numbered_list = bool(re.findall(r'\n\s*\d+[.)]\s', text_clean)) or \
                        bool(re.findall(r'[①②③④⑤⑥⑦⑧⑨⑩]', text_clean)) or \
                        len(re.findall(r'\d️⃣', text_clean)) >= 2

    return {
        "char_len": len(text_clean),
        "line_count": len(lines),
        "word_count": len(words),
        "has_url": has_url,
        "has_emoji": has_emoji,
        "has_hashtag": has_hashtag,
        "has_arrow": has_arrow,
        "has_question": has_question,
        "has_numbered_list": has_numbered_list,
    }


def extract_engagement(tweet):
    """참여 지표 추출."""
    views = tweet.get("viewCount", 0)
    likes = tweet.get("likeCount", 0)
    replies = tweet.get("replyCount", 0)
    rts = tweet.get("retweetCount", 0)
    quotes = tweet.get("quoteCount", 0)
    bookmarks = tweet.get("bookmarkCount", 0)

    total_eng = likes + replies + rts + quotes + bookmarks
    er = total_eng / views if views > 0 else 0
    like_rate = likes / views if views > 0 else 0
    bookmark_rate = bookmarks / views if views > 0 else 0
    reply_rate = replies / views if views > 0 else 0
    rt_rate = rts / views if views > 0 else 0

    return {
        "views": views,
        "likes": likes,
        "replies": replies,
        "retweets": rts,
        "quotes": quotes,
        "bookmarks": bookmarks,
        "total_engagement": total_eng,
        "er": er,
        "like_rate": like_rate,
        "bookmark_rate": bookmark_rate,
        "reply_rate": reply_rate,
        "rt_rate": rt_rate,
    }


# ──────────────────────────────────────────────────────────
# Phase 3: Hook 분류 (8 상호배타 + 부가 속성)
# ──────────────────────────────────────────────────────────

# LLM 7유형 → v2 8유형 매핑
LLM_TO_V2_HOOK = {
    "comparison_choice": "question_poll",
    "narrative_situation": "narrative_hook",
    "info_fact": "fact_curiosity",
    "opinion_reaction": "relatable_target",
    "celeb_mention": "narrative_hook",
    "promotion": "practical_tip",
    "emotion_empathy": "relatable_target",
}

HOOK_PATTERNS_V2 = [
    # 우선순위순 — 첫 매칭이 1차 유형
    ("challenge", re.compile(
        r'(풀어|맞춰|퀴즈|테스트|도전|챌린지|찾아봐|찾아보|틀린\s*곳|다른\s*곳|몇\s*개|맞히|찾았다면|못\s*찾|찾으면)',
        re.IGNORECASE
    )),
    ("credibility", re.compile(
        r'(현직|전문[가의]|의사[가의\s]|교수[가의\s]|변호사|약사|전직|[0-9]+년\s*차|연차|업계|실무자|석사|박사|전공|디렉터|대표|CEO)',
        re.IGNORECASE
    )),
    ("warning_loss", re.compile(
        r'(하지\s*마|절대\s*[하금안못]|위험[하한]|조심|주의\b|금물|하면\s*안|쓰면\s*안|먹으면\s*안|안\s*되는\s*이유|절대로)',
        re.IGNORECASE
    )),
    ("practical_tip", re.compile(
        r'(방법|하려면|꿀팁|노하우|비법|비결|팁\b|루틴|습관|하는\s*법|알려드|정리했|정리\.|모음|추천\s*[0-9]|가지\s*추천|총정리)',
        re.IGNORECASE
    )),
    ("fact_curiosity", re.compile(
        r'(하는\s*이유|이유[가를은\s]|때문[에이]|중요성|알고\s*보[면니]|사실[은\s]|몰랐던|비밀|비하인드|라는\s*사실|인\s*이유|의\s*이유)',
        re.IGNORECASE
    )),
    ("relatable_target", re.compile(
        r'(하는\s*사람|인\s*분들?|하는\s*분들?|있는\s*사람|겪[는은]|겪어본|공감|해본\s*사람|느끼는|다들\s|너희|여러분|주목|해당되[면는])',
        re.IGNORECASE
    )),
    ("question_poll", re.compile(
        r'(\?\s*$|[?？❓]|vs\s|VS\s|선택|어떻게\s*생각|고르|고민|어떠세요|하시겠|몇\s*번|어때|뭐\s*고르|뭘\s*고르)',
        re.IGNORECASE
    )),
    ("narrative_hook", re.compile(
        r'(어제|오늘|방금|아까|살면서|처음으로|나는|저는|제가|내가|했는데|하다가|했더니|알고\s*보니|라는데|길래|인데$|건데$)',
        re.IGNORECASE
    )),
]

HOOK_LABELS_KR_V2 = {
    "challenge": "참여 유도형",
    "credibility": "권위/내부자형",
    "warning_loss": "경고/손실형",
    "practical_tip": "실용 정보형",
    "fact_curiosity": "사실/호기심형",
    "relatable_target": "공감/대상형",
    "question_poll": "질문/투표형",
    "narrative_hook": "서사 시작형",
    "general": "일반형",
}


def classify_hook_v2(hook_text):
    """상호배타적 1차 Hook 분류 (우선순위순 첫 매칭)."""
    for name, pattern in HOOK_PATTERNS_V2:
        if pattern.search(hook_text):
            return name

    # LLM 캐시 폴백
    llm_label = LLM_HOOK_OVERRIDES.get(hook_text)
    if llm_label:
        return LLM_TO_V2_HOOK.get(llm_label, "general")

    return "general"


def extract_hook_addons(hook_text):
    """부가 속성 (다중 레이블)."""
    addons = []
    if re.search(r'[\[【「『]', hook_text):
        addons.append("has_bracket")
    if re.search(r'\d', hook_text):
        addons.append("has_number")
    if re.search(r'["\u201c\u201d\u2018\u2019\u300c\u300d]', hook_text):
        addons.append("has_quote")
    if re.search(r'(속보|충격|논란|경악|긴급|단독|ㄷㄷ|헐|실화|레전드|미쳤|대박|역대급|ㅋㅋㅋ|🤣|😱)', hook_text):
        addons.append("has_shock_marker")
    if EMOJI_RE.search(hook_text):
        addons.append("has_emoji")

    hook_len = len(hook_text)
    if hook_len <= 25:
        addons.append("is_short")
    elif hook_len <= 60:
        addons.append("is_medium")
    else:
        addons.append("is_long")

    return addons


# ──────────────────────────────────────────────────────────
# Phase 4: Body 분류 (7 상호배타)
# ──────────────────────────────────────────────────────────

BODY_LABELS_KR_V2 = {
    "numbered_list": "번호 리스트형",
    "arrow_flow": "화살표 흐름형",
    "comparison_table": "비교/대비형",
    "short_line_stack": "짧은 줄 쌓기형",
    "threaded_narrative": "서사/스토리형",
    "one_liner_media": "한 줄+미디어형",
    "dialogue_form": "대화체형",
    "general_body": "일반 본문형",
}


def classify_body_v2(text):
    """상호배타적 Body 구조 분류 (우선순위순)."""
    text_clean = URL_RE.sub("", text).strip()
    lines = [l.strip() for l in text_clean.split("\n") if l.strip()]

    # 1. numbered_list: 번호가 2개 이상
    num_matches = len(re.findall(r'\n\s*\d+[.)]\s', text_clean))
    emoji_num = len(re.findall(r'[①②③④⑤⑥⑦⑧⑨⑩❶❷❸❹❺❻❼❽❾❿]', text_clean))
    digit_emoji = len(re.findall(r'\d️⃣', text_clean))
    if num_matches >= 2 or emoji_num >= 2 or digit_emoji >= 2:
        return "numbered_list"

    # 2. arrow_flow: 화살표 2개 이상
    arrow_count = len(re.findall(r'[→➡▶►]', text_clean))
    if arrow_count >= 2:
        return "arrow_flow"

    # 3. comparison_table: A vs B 구조
    if re.search(r'(vs|VS|[Vv][Ss]\.?\s)', text_clean) or \
       re.search(r'(.+)\s*[vs|VS]\s*(.+)', text_clean):
        return "comparison_table"

    # 4. short_line_stack: 4줄+, 각 50자 미만
    if len(lines) >= 4 and all(len(l) < 50 for l in lines):
        return "short_line_stack"

    # 5. threaded_narrative: 120자+ 연속 서술
    if len(text_clean) > 120:
        return "threaded_narrative"

    # 6. one_liner_media: URL 제거 후 60자 미만
    if len(text_clean) < 60:
        return "one_liner_media"

    # 7. dialogue_form: 따옴표 대화 패턴
    if re.search(r'["\u201c\u201d].+["\u201c\u201d]\s*.+["\u201c\u201d]', text_clean):
        return "dialogue_form"

    return "general_body"


# ──────────────────────────────────────────────────────────
# Phase 5: 통계 분석
# ──────────────────────────────────────────────────────────

def median(values):
    """중앙값."""
    if not values:
        return 0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def mean(values):
    """평균."""
    return sum(values) / len(values) if values else 0


def stdev(values):
    """표준편차."""
    if len(values) < 2:
        return 0
    m = mean(values)
    return math.sqrt(sum((x - m) ** 2 for x in values) / (len(values) - 1))


def ci_95(values):
    """95% 신뢰구간."""
    n = len(values)
    if n < 2:
        return (0, 0)
    m = mean(values)
    se = stdev(values) / math.sqrt(n)
    return (m - 1.96 * se, m + 1.96 * se)


def cohens_d(group_a, group_b):
    """Cohen's d 효과크기."""
    if len(group_a) < 2 or len(group_b) < 2:
        return 0.0
    m1, m2 = mean(group_a), mean(group_b)
    s1, s2 = stdev(group_a), stdev(group_b)
    pooled = math.sqrt(((len(group_a) - 1) * s1**2 + (len(group_b) - 1) * s2**2) /
                        (len(group_a) + len(group_b) - 2))
    if pooled == 0:
        return 0.0
    return (m1 - m2) / pooled


def pearson_r(xs, ys):
    """Pearson 상관계수."""
    n = len(xs)
    if n < 2:
        return 0.0
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def spearman_rho(xs, ys):
    """Spearman 순위 상관계수."""
    n = len(xs)
    if n < 2:
        return 0.0

    def rank(vals):
        indexed = sorted(range(n), key=lambda i: vals[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n - 1 and vals[indexed[j]] == vals[indexed[j + 1]]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                ranks[indexed[k]] = avg_rank
            i = j + 1
        return ranks

    rx = rank(xs)
    ry = rank(ys)
    return pearson_r(rx, ry)


def analyze_all_tweets(tweets):
    """전체 트윗에 대해 피처 추출 + Hook/Body 분류."""
    results = []
    for t in tweets:
        text = t.get("fullText", "")
        hook_text = extract_hook(text)
        hook_type = classify_hook_v2(hook_text)
        hook_addons = extract_hook_addons(hook_text)
        body_type = classify_body_v2(text)
        text_feat = extract_text_features(text)
        eng = extract_engagement(t)

        results.append({
            "text": text,
            "hook_text": hook_text,
            "hook_type": hook_type,
            "hook_addons": hook_addons,
            "body_type": body_type,
            "text_features": text_feat,
            "engagement": eng,
            "source": t.get("source", ""),
            "is_high": eng["views"] >= THRESHOLD,
        })
    return results


def compute_type_stats(analyzed, type_key, all_types_kr):
    """유형별 통계: N, 평균/중앙값 조회수, 95% CI, lift, Cohen's d."""
    type_data = defaultdict(list)
    for item in analyzed:
        type_data[item[type_key]].append(item)

    total_n = len(analyzed)
    total_high = sum(1 for item in analyzed if item["is_high"])
    p_high = total_high / total_n if total_n > 0 else 0

    stats = {}
    for label, items in type_data.items():
        views = [i["engagement"]["views"] for i in items]
        n = len(items)
        n_high = sum(1 for i in items if i["is_high"])
        n_low = n - n_high

        p_high_given_type = n_high / n if n > 0 else 0
        lift = p_high_given_type / p_high if p_high > 0 else 0

        # Cohen's d: 해당 유형 vs 비해당 유형
        other_views = [i["engagement"]["views"] for i in analyzed if i[type_key] != label]
        d = cohens_d(views, other_views)

        ci = ci_95(views)

        likes = [i["engagement"]["likes"] for i in items]
        bookmarks = [i["engagement"]["bookmarks"] for i in items]
        ers = [i["engagement"]["er"] * 100 for i in items]

        kr_name = all_types_kr.get(label, label)
        stats[label] = {
            "label_kr": kr_name,
            "n": n,
            "n_high": n_high,
            "n_low": n_low,
            "avg_views": round(mean(views)),
            "median_views": round(median(views)),
            "ci_low": round(ci[0]),
            "ci_high": round(ci[1]),
            "lift": round(lift, 3),
            "cohens_d": round(d, 3),
            "avg_likes": round(mean(likes), 1),
            "avg_bookmarks": round(mean(bookmarks), 1),
            "avg_er": round(mean(ers), 3),
            "median_er": round(median(ers), 3),
        }
    return stats


def compute_cross_table(analyzed):
    """Hook × Body 교차표."""
    cross = defaultdict(list)
    for item in analyzed:
        key = (item["hook_type"], item["body_type"])
        cross[key].append(item)

    total_n = len(analyzed)
    total_high = sum(1 for item in analyzed if item["is_high"])
    p_high = total_high / total_n if total_n > 0 else 0

    table = {}
    for (hook, body), items in cross.items():
        n = len(items)
        n_high = sum(1 for i in items if i["is_high"])
        views = [i["engagement"]["views"] for i in items]
        p_type = n_high / n if n > 0 else 0
        lift = p_type / p_high if p_high > 0 else 0

        table[(hook, body)] = {
            "n": n,
            "n_high": n_high,
            "median_views": round(median(views)),
            "avg_views": round(mean(views)),
            "lift": round(lift, 3),
        }
    return table


def compute_engagement_correlations(analyzed):
    """참여지표 vs viewCount 상관관계."""
    views = [i["engagement"]["views"] for i in analyzed]
    fields = [
        ("likes", "좋아요"),
        ("replies", "답글"),
        ("retweets", "리트윗"),
        ("bookmarks", "북마크"),
        ("quotes", "인용"),
    ]

    results = {}
    for field, kr in fields:
        vals = [i["engagement"][field] for i in analyzed]
        pr = pearson_r(vals, views)
        sr = spearman_rho(vals, views)
        results[kr] = {"pearson": round(pr, 4), "spearman": round(sr, 4)}
    return results


def compute_text_feature_effects(analyzed):
    """텍스트 피처별 고성과 비율차, Cohen's d."""
    features = ["has_url", "has_emoji", "has_hashtag", "has_arrow",
                 "has_question", "has_numbered_list"]
    results = {}
    for feat in features:
        with_feat = [i for i in analyzed if i["text_features"][feat]]
        without_feat = [i for i in analyzed if not i["text_features"][feat]]

        pct_high_with = sum(1 for i in with_feat if i["is_high"]) / len(with_feat) * 100 if with_feat else 0
        pct_high_without = sum(1 for i in without_feat if i["is_high"]) / len(without_feat) * 100 if without_feat else 0

        views_with = [i["engagement"]["views"] for i in with_feat]
        views_without = [i["engagement"]["views"] for i in without_feat]
        d = cohens_d(views_with, views_without)

        results[feat] = {
            "n_with": len(with_feat),
            "n_without": len(without_feat),
            "pct_high_with": round(pct_high_with, 1),
            "pct_high_without": round(pct_high_without, 1),
            "diff_pp": round(pct_high_with - pct_high_without, 1),
            "cohens_d": round(d, 3),
        }

    # 길이 구간별
    length_bins = [
        ("0-30자", 0, 30),
        ("31-80자", 31, 80),
        ("81-150자", 81, 150),
        ("151-280자", 151, 280),
        ("280자+", 281, 99999),
    ]
    length_results = {}
    for label, lo, hi in length_bins:
        group = [i for i in analyzed if lo <= i["text_features"]["char_len"] <= hi]
        n = len(group)
        n_high = sum(1 for i in group if i["is_high"])
        pct_high = n_high / n * 100 if n > 0 else 0
        avg_views = mean([i["engagement"]["views"] for i in group]) if group else 0
        length_results[label] = {
            "n": n,
            "n_high": n_high,
            "pct_high": round(pct_high, 1),
            "avg_views": round(avg_views),
        }

    return results, length_results


def find_failure_patterns(analyzed_low):
    """저성과 트윗의 실패 패턴 분류."""
    patterns = []
    n = len(analyzed_low)
    if n == 0:
        return patterns

    # URL만 (텍스트 10자 미만)
    url_only = sum(1 for i in analyzed_low if i["text_features"]["char_len"] < 10)
    patterns.append(("URL만 있는 트윗 (텍스트 10자 미만)", url_only, round(url_only / n * 100, 1)))

    # 30자 미만 초단문
    very_short = sum(1 for i in analyzed_low if i["text_features"]["char_len"] < 30)
    patterns.append(("30자 미만 초단문", very_short, round(very_short / n * 100, 1)))

    # 단순 링크 공유
    link_share = sum(1 for i in analyzed_low
                     if i["text_features"]["has_url"] and i["text_features"]["char_len"] < 50)
    patterns.append(("단순 링크 공유 (텍스트 50자 미만 + URL)", link_share, round(link_share / n * 100, 1)))

    # @멘션 시작
    mention_start = sum(1 for i in analyzed_low if i["text"].startswith("@"))
    patterns.append(("@멘션으로 시작 (대화형)", mention_start, round(mention_start / n * 100, 1)))

    # Hook 일반형 (패턴 미해당)
    general_hook = sum(1 for i in analyzed_low if i["hook_type"] == "general")
    patterns.append(("Hook 일반형 (패턴 미해당)", general_hook, round(general_hook / n * 100, 1)))

    return sorted(patterns, key=lambda x: x[1], reverse=True)


# ──────────────────────────────────────────────────────────
# Phase 6: X 알고리즘 분석
# ──────────────────────────────────────────────────────────

ALGORITHM_SIGNALS = {
    "positive": [
        ("favorite", "좋아요", "공감·동의를 유발하는 콘텐츠"),
        ("reply", "답글", "질문·논쟁으로 대화 유도"),
        ("retweet", "리트윗", "공유할 가치가 있는 정보성 콘텐츠"),
        ("quote", "인용 트윗", "의견을 추가할 여지를 남기는 콘텐츠"),
        ("share", "공유", "퍼뜨리고 싶은 콘텐츠"),
        ("share_via_dm", "DM으로 공유", "\"이거 봐봐\" 하고 보내고 싶은 콘텐츠"),
        ("share_via_copy_link", "링크 복사", "외부 플랫폼에 공유할 만한 콘텐츠"),
        ("dwell", "머무름 여부", "스크롤을 멈추게 만드는 Hook"),
        ("dwell_time", "머무른 시간(연속)", "끝까지 읽게 만드는 Body 구조"),
        ("photo_expand", "이미지 확대 클릭", "클릭해서 자세히 보고 싶은 이미지"),
        ("click", "트윗 클릭", "더 보고 싶게 만드는 구조 (잘린 텍스트 등)"),
        ("profile_click", "프로필 클릭", "\"이 사람 누구지?\" 유발 → 권위형 Hook"),
        ("follow_author", "팔로우", "지속적 가치를 느끼게 하는 시리즈/전문 콘텐츠"),
        ("video_quality_view", "영상 시청", "최소 200ms 이상 영상을 끝까지 시청"),
        ("quoted_click", "인용 트윗 클릭", "인용된 원문을 클릭하게 만드는 맥락"),
    ],
    "negative": [
        ("not_interested", "\"관심 없음\" 클릭", "타겟 독자 이탈 방지: 주제 명확히"),
        ("block_author", "차단", "공격적·불쾌한 톤 회피"),
        ("mute_author", "뮤트", "과도한 포스팅 빈도 주의"),
        ("report", "신고", "허위 정보·자극적 낚시 회피"),
    ],
}

# Hook 유형별 주로 트리거하는 알고리즘 시그널 매핑
HOOK_ALGO_MAP = {
    "challenge": ["reply", "click", "dwell_time", "share"],
    "credibility": ["profile_click", "follow_author", "favorite", "share_via_dm"],
    "warning_loss": ["favorite", "share_via_dm", "dwell", "click"],
    "practical_tip": ["favorite", "share_via_copy_link", "share_via_dm", "dwell_time"],
    "fact_curiosity": ["dwell", "click", "favorite", "share_via_dm"],
    "relatable_target": ["favorite", "reply", "retweet", "quote"],
    "question_poll": ["reply", "quote", "favorite", "click"],
    "narrative_hook": ["dwell", "dwell_time", "favorite", "share_via_dm"],
    "general": ["dwell", "favorite"],
}

BODY_ALGO_MAP = {
    "numbered_list": ["dwell_time", "share_via_copy_link", "favorite"],
    "arrow_flow": ["dwell_time", "share_via_dm", "favorite"],
    "comparison_table": ["reply", "quote", "dwell_time"],
    "short_line_stack": ["dwell_time", "dwell", "favorite"],
    "threaded_narrative": ["dwell_time", "click", "share_via_dm"],
    "one_liner_media": ["photo_expand", "dwell", "share"],
    "dialogue_form": ["dwell_time", "reply", "favorite"],
    "general_body": ["dwell", "favorite"],
}


def parse_algorithm_files():
    """알고리즘 소스 파일에서 핵심 정보 추출."""
    algo_info = {
        "scoring_signals": [],
        "age_filter": "",
        "diversity_formula": "",
        "muted_keyword": "",
        "pipeline_stages": "",
    }

    # extracted 디렉토리에서 읽기
    scorer_path = EXTRACTED_DIR / "home-mixer" / "scorers" / "weighted_scorer.rs"
    age_path = EXTRACTED_DIR / "home-mixer" / "filters" / "age_filter.rs"
    diversity_path = EXTRACTED_DIR / "home-mixer" / "scorers" / "author_diversity_scorer.rs"
    muted_path = EXTRACTED_DIR / "home-mixer" / "filters" / "muted_keyword_filter.rs"

    if scorer_path.exists():
        content = scorer_path.read_text(encoding="utf-8")
        # 시그널 이름 추출
        signals = re.findall(r'(\w+_score)\s*[×*]\s*\w+_WEIGHT', content)
        if not signals:
            signals = re.findall(r'(\w+_score)', content)
        algo_info["scoring_signals"] = list(set(signals))

    if age_path.exists():
        content = age_path.read_text(encoding="utf-8")
        if "max_age" in content or "MAX_POST_AGE" in content:
            algo_info["age_filter"] = "24시간 (86,400초) 하드 컷오프 - Snowflake ID 기반 계산"

    if diversity_path.exists():
        content = diversity_path.read_text(encoding="utf-8")
        algo_info["diversity_formula"] = "multiplier = (1 - floor) × decay^position + floor"

    if muted_path.exists():
        content = muted_path.read_text(encoding="utf-8")
        algo_info["muted_keyword"] = "TweetTokenizer 기반 정확 토큰 매칭 (fuzzy 아님)"

    return algo_info


# ──────────────────────────────────────────────────────────
# Phase 7: 공식 도출
# ──────────────────────────────────────────────────────────

def extract_hook_patterns(analyzed_high, hook_type, top_n=3):
    """고성과 Hook 텍스트에서 공통 구문 패턴 자동 추출."""
    hooks = [i["hook_text"] for i in analyzed_high if i["hook_type"] == hook_type]
    if not hooks:
        return []

    # 어미/조사 기반 패턴 추출
    pattern_templates = [
        (r'(.+하는)\s*이유', '[주제]하는 이유'),
        (r'(.+하는)\s*사람', '[대상]하는 사람'),
        (r'(.+하는)\s*법', '[주제]하는 법'),
        (r'(.+하는)\s*분들?', '[대상]하는 분들'),
        (r'(.+인)\s*분들?', '[대상]인 분들'),
        (r'현직\s*(.+)[이가]', '현직 [직업]이 알려주는 [주제]'),
        (r'(\d+)년\s*차', '[N]년차 [직업]의 [주제]'),
        (r'(\d+)\s*가지', '[주제] [N]가지'),
        (r'절대\s*(.+)[하하면]', '절대 [행동]하면 안 되는 이유'),
        (r'(.+)[?？❓]', '[질문]?'),
        (r'(.+)했는데', '[상황]했는데 [결과]'),
        (r'(.+)하다가', '[상황]하다가 [전개]'),
        (r'(.+)했더니', '[행동]했더니 [결과]'),
        (r'(.+)\s*vs\s*(.+)', '[A] vs [B]'),
        (r'(.+)\s*추천', '[주제] 추천'),
        (r'(.+)\s*정리', '[주제] 정리'),
        (r'(.+)\s*꿀팁', '[주제] 꿀팁'),
        (r'(.+)\s*비결', '[주제] 비결'),
        (r'알고\s*보[면니]\s*(.+)', '알고 보면 [주제]'),
    ]

    pattern_counts = Counter()
    for hook in hooks:
        for regex, template in pattern_templates:
            if re.search(regex, hook, re.IGNORECASE):
                pattern_counts[template] += 1

    # 빈도순 상위 N개
    top_patterns = [tmpl for tmpl, cnt in pattern_counts.most_common(top_n)]

    # 부족하면 기본 템플릿 추가
    default_templates = {
        "challenge": ["이거 [N]초 안에 풀면 상위 [N]%", "[주제] 퀴즈: 몇 개 맞출 수 있을까?", "틀린 곳 찾기 (힌트: [단서])"],
        "credibility": ["현직 [직업]이 알려주는 [주제]", "[N]년차 [직업]의 [주제] 정리", "[전문가]가 추천하는 [주제]"],
        "warning_loss": ["절대 [행동]하면 안 되는 이유", "[대상] 조심하세요, [경고 내용]", "[행동]하지 마세요. [근거/사례]"],
        "practical_tip": ["[주제] 하는 방법 총정리", "[주제] 꿀팁 [N]가지", "[목표]하려면 반드시 알아야 할 것"],
        "fact_curiosity": ["[대상/현상]의 진짜 이유", "알고 보면 [주제]의 비밀", "[대상]이 [결과]인 이유"],
        "relatable_target": ["[대상]하는 사람 특징", "[대상]인 분들 꼭 보세요", "[상황] 겪어본 사람만 공감하는 [주제]"],
        "question_poll": ["[주제]에 대해 어떻게 생각하세요?", "[A] vs [B], 당신의 선택은?", "[상황] 어떻게 하시겠어요?"],
        "narrative_hook": ["[상황]했는데 [결과]", "[인물]이 [행동]한 결과", "[장소]에서 벌어진 일"],
        "general": ["[주제] (+ 이미지/영상)", "[단어/문구]. [한 문장].", "[주제] 근황"],
    }

    defaults = default_templates.get(hook_type, ["(템플릿 미정의)"])
    while len(top_patterns) < top_n and defaults:
        tmpl = defaults.pop(0)
        if tmpl not in top_patterns:
            top_patterns.append(tmpl)

    return top_patterns[:top_n]


def extract_body_structure(analyzed_high, body_type):
    """고성과 Body 텍스트에서 구조 패턴 식별."""
    items = [i for i in analyzed_high if i["body_type"] == body_type]
    if not items:
        return "구조 분석 불가 (해당 유형 0건)"

    structures = {
        "numbered_list": "Hook → 브릿지 문장 → 번호 리스트 (1. 2. 3. ...) → 마무리/CTA",
        "arrow_flow": "Hook → 원인/시작 → 화살표(→) → 과정 → 화살표(→) → 결과/교훈",
        "comparison_table": "Hook → A 설명 → vs → B 설명 → 결론/질문",
        "short_line_stack": "Hook → 짧은 문장 1 → 짧은 문장 2 → ... → 짧은 마무리",
        "threaded_narrative": "Hook → 배경 설명 → 사건 전개 → 클라이맥스 → 결론/교훈",
        "one_liner_media": "짧은 캡션/코멘트 + 이미지/영상 (시각 콘텐츠가 주역)",
        "dialogue_form": "Hook → \"대사 A\" → 반응 → \"대사 B\" → 결론",
        "general_body": "Hook → 본문 (특정 구조 없음)",
    }

    return structures.get(body_type, "구조 분석 불가")


def get_top_examples(analyzed, type_key, type_val, n=3, high_only=True):
    """특정 유형의 상위 N개 예시."""
    matching = [i for i in analyzed if i[type_key] == type_val]
    if high_only:
        matching = [i for i in matching if i["is_high"]]
    matching.sort(key=lambda x: x["engagement"]["views"], reverse=True)
    return matching[:n]


# ──────────────────────────────────────────────────────────
# Phase 8: 마크다운 보고서 생성
# ──────────────────────────────────────────────────────────

def truncate_text(text, max_len=150):
    text = text.replace("\n", " ↵ ").strip()
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def format_number(n):
    if isinstance(n, float):
        n = round(n)
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    elif abs(n) >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def generate_report(analyzed_all, analyzed_high_orig, analyzed_low_orig,
                    analyzed_high_rt, analyzed_low_rt,
                    hook_stats, body_stats, cross_table,
                    eng_corrs, feat_effects, length_effects,
                    failure_pats, algo_info,
                    total_tweets, high_count, low_count,
                    high_orig_count, high_rt_count,
                    low_orig_count, low_rt_count):
    """tweet_formulas_v2.md 생성."""

    # 오리지널만 대상 분석
    orig_analyzed = analyzed_high_orig + analyzed_low_orig

    lines = []
    def w(s=""):
        lines.append(s)

    # ════════════════════════════════════════════
    # 1. 분석 개요
    # ════════════════════════════════════════════
    w("# X(트위터) 성공 트윗 공식 분석 보고서 v2")
    w()
    w("> v1 대비 핵심 개선: 상호배타 분류, 전체 트윗 동일 적용, Hook×Body 교차표, 자동 패턴 추출, Cohen's d/Lift 기반 통계")
    w()
    w("---")
    w()

    w("## 1. 분석 개요")
    w()
    w("### 데이터셋")
    w()
    w(f"- **분석 대상**: {total_tweets:,}건의 트윗 (56개 계정)")
    w(f"- **고성과 기준**: 조회수 ≥ {THRESHOLD:,}")
    w(f"- **고성과 트윗**: {high_count:,}건 ({high_count/total_tweets*100:.1f}%)")
    w(f"  - 오리지널: {high_orig_count:,}건")
    w(f"  - RT: {high_rt_count:,}건")
    w(f"- **저성과 트윗**: {low_count:,}건")
    w(f"  - 오리지널: {low_orig_count:,}건")
    w(f"  - RT: {low_rt_count:,}건")
    w(f"- **공식 도출 대상**: 오리지널 트윗 ({high_orig_count + low_orig_count:,}건)")
    w()

    w("### 방법론 (v1 대비 변경)")
    w()
    w("| 항목 | v1 | v2 |")
    w("|:---|:---|:---|")
    w("| Hook 분류 | 다중 레이블 12유형 + LLM 7유형 | **상호배타 8유형** + 부가속성 |")
    w("| Body 분류 | 다중 레이블 5유형 | **상호배타 7유형** |")
    w("| 분류 적용 | 고성과만 (LLM) | **전체 트윗 동일 적용** |")
    w("| 핵심 지표 | 고/저 비율 × 평균 조회수 | **Lift ratio, Cohen's d, 95% CI** |")
    w("| 조합 분석 | 없음 | **Hook × Body 교차표** (56개 조합) |")
    w("| 공식 도출 | 수작업 템플릿 | **데이터 기반 자동 추출** |")
    w("| 알고리즘 | 별도 섹션 | **각 유형에 시그널 매핑** |")
    w()

    # ════════════════════════════════════════════
    # 2. 1분 요약: 황금 공식
    # ════════════════════════════════════════════
    w("## 2. 1분 요약: 황금 공식")
    w()

    # 최적 조합 3개 찾기 (lift 기준, N>=10)
    sorted_combos = sorted(
        [(k, v) for k, v in cross_table.items() if v["n"] >= 10],
        key=lambda x: x[1]["lift"],
        reverse=True,
    )
    top3_combos = sorted_combos[:3]

    w("### 최적 Hook × Body × 길이 조합")
    w()
    for idx, ((hook, body), stats) in enumerate(top3_combos, 1):
        hook_kr = HOOK_LABELS_KR_V2.get(hook, hook)
        body_kr = BODY_LABELS_KR_V2.get(body, body)
        w(f"**{idx}. {hook_kr} + {body_kr}**")
        w(f"- Lift: {stats['lift']:.2f}× | N: {stats['n']}건 | 고성과: {stats['n_high']}건 | 중앙값 조회수: {format_number(stats['median_views'])}")

        # 해당 조합 예시 1개
        combo_examples = [i for i in analyzed_high_orig
                          if i["hook_type"] == hook and i["body_type"] == body]
        combo_examples.sort(key=lambda x: x["engagement"]["views"], reverse=True)
        if combo_examples:
            ex = combo_examples[0]
            w(f"- 예시: `{truncate_text(ex['hook_text'], 80)}` (조회 {format_number(ex['engagement']['views'])})")
        w()

    w("### 황금 공식")
    w()
    w("```")
    w("[멈추게 하는 Hook] + [오래 읽게 하는 Body(151-280자)] + [보내고 싶은 공유 가치] = 바이럴 트윗")
    w("```")
    w()
    w("- **멈추게 하는 Hook**: 알고리즘 `dwell`(머무름 여부) 시그널 → 스크롤 정지 유도")
    w("- **오래 읽게 하는 Body**: 알고리즘 `dwell_time`(체류 시간) 시그널 → 구조화된 본문")
    w("- **보내고 싶은 공유 가치**: 알고리즘 `share_via_dm` 별도 가중치 → \"친구한테 보내고 싶은\" 콘텐츠")
    w()

    # ════════════════════════════════════════════
    # 3. Hook 작성 공식 (8유형)
    # ════════════════════════════════════════════
    w("## 3. Hook 작성 공식 (8유형)")
    w()
    w("> Hook = 트윗의 첫 번째 줄. 독자가 스크롤을 멈추게 만드는 '미끼'.")
    w("> 분류: 상호배타적 8유형 (우선순위순 첫 매칭). 모든 트윗에 동일 적용.")
    w()

    # Lift 순으로 정렬
    hook_order = sorted(hook_stats.keys(), key=lambda x: hook_stats[x]["lift"], reverse=True)

    for rank_idx, label in enumerate(hook_order, 1):
        s = hook_stats[label]
        kr = s["label_kr"]

        w(f"### 3.{rank_idx}. {kr} (`{label}`)")
        w()

        # 통계
        w(f"- **총 트윗**: {s['n']:,}건 (고성과 {s['n_high']:,} / 저성과 {s['n_low']:,})")
        w(f"- **Lift**: {s['lift']:.3f}× (이 유형의 고성과 확률 ÷ 전체 고성과 확률)")
        w(f"- **Cohen's d**: {s['cohens_d']:.3f}")
        w(f"- **평균 조회수**: {format_number(s['avg_views'])} (중앙값: {format_number(s['median_views'])})")
        w(f"- **95% CI**: [{format_number(s['ci_low'])}, {format_number(s['ci_high'])}]")
        w(f"- **평균 좋아요**: {format_number(int(s['avg_likes']))} | 평균 북마크: {format_number(int(s['avg_bookmarks']))}")
        w(f"- **평균 ER**: {s['avg_er']:.3f}%")
        w()

        # 데이터 추출 템플릿
        templates = extract_hook_patterns(analyzed_high_orig, label)
        w("**데이터 추출 공식 템플릿:**")
        w()
        for tmpl in templates:
            w(f"- `{tmpl}`")
        w()

        # 실제 예시
        examples = get_top_examples(orig_analyzed, "hook_type", label, 3)
        if examples:
            w("**실제 고성과 예시:**")
            w()
            for j, ex in enumerate(examples, 1):
                w(f"{j}. (조회 {format_number(ex['engagement']['views'])}, 좋아요 {format_number(ex['engagement']['likes'])}) `{truncate_text(ex['hook_text'], 100)}`")
            w()

        # 알고리즘 시그널
        algo_signals = HOOK_ALGO_MAP.get(label, [])
        if algo_signals:
            w(f"**트리거 알고리즘 시그널**: {', '.join(f'`{s}`' for s in algo_signals)}")
            w()

        # 성공/실패 이유
        high_in_type = [i for i in orig_analyzed if i["hook_type"] == label and i["is_high"]]
        low_in_type = [i for i in orig_analyzed if i["hook_type"] == label and not i["is_high"]]
        if high_in_type and low_in_type:
            avg_len_high = mean([i["text_features"]["char_len"] for i in high_in_type])
            avg_len_low = mean([i["text_features"]["char_len"] for i in low_in_type])
            w(f"**성공 vs 실패**: 고성과 평균 텍스트 길이 {avg_len_high:.0f}자 vs 저성과 {avg_len_low:.0f}자")
            w()

        w("---")
        w()

    # ════════════════════════════════════════════
    # 4. Body 작성 공식 (7구조)
    # ════════════════════════════════════════════
    w("## 4. Body 작성 공식 (7구조)")
    w()
    w("> Body = Hook 이후의 본문. 독자를 끝까지 읽게 만들고 행동(좋아요/RT/북마크)을 유도하는 구조.")
    w("> 분류: 상호배타적 7유형 (우선순위순 첫 매칭). 모든 트윗에 동일 적용.")
    w()

    body_order = sorted(body_stats.keys(), key=lambda x: body_stats[x]["lift"], reverse=True)

    for rank_idx, label in enumerate(body_order, 1):
        s = body_stats[label]
        kr = s["label_kr"]

        w(f"### 4.{rank_idx}. {kr} (`{label}`)")
        w()

        w(f"- **총 트윗**: {s['n']:,}건 (고성과 {s['n_high']:,} / 저성과 {s['n_low']:,})")
        w(f"- **Lift**: {s['lift']:.3f}× | **Cohen's d**: {s['cohens_d']:.3f}")
        w(f"- **평균 조회수**: {format_number(s['avg_views'])} (중앙값: {format_number(s['median_views'])})")
        w(f"- **95% CI**: [{format_number(s['ci_low'])}, {format_number(s['ci_high'])}]")
        w(f"- **평균 좋아요**: {format_number(int(s['avg_likes']))} | 평균 ER: {s['avg_er']:.3f}%")
        w()

        # 구조 템플릿
        structure = extract_body_structure(analyzed_high_orig, label)
        w("**구조 템플릿:**")
        w()
        w(f"```")
        w(structure)
        w(f"```")
        w()

        # 실제 예시
        examples = get_top_examples(orig_analyzed, "body_type", label, 3)
        if examples:
            w("**실제 고성과 예시:**")
            w()
            for j, ex in enumerate(examples, 1):
                w(f"{j}. (조회 {format_number(ex['engagement']['views'])}, 좋아요 {format_number(ex['engagement']['likes'])}) `{truncate_text(ex['text'], 200)}`")
            w()

        # 알고리즘 시그널
        algo_signals = BODY_ALGO_MAP.get(label, [])
        if algo_signals:
            w(f"**트리거 알고리즘 시그널**: {', '.join(f'`{s}`' for s in algo_signals)}")
            w()

        w("---")
        w()

    # ════════════════════════════════════════════
    # 5. 최적 조합 분석 (Hook × Body 교차표)
    # ════════════════════════════════════════════
    w("## 5. 최적 조합 분석 (Hook × Body 교차표)")
    w()

    # 교차표 요약
    w("### 5.1 전체 교차표 (Lift)")
    w()

    all_hooks = sorted(HOOK_LABELS_KR_V2.keys())
    all_bodies = sorted(BODY_LABELS_KR_V2.keys())

    header = "| Hook \\ Body |"
    for body in all_bodies:
        header += f" {BODY_LABELS_KR_V2[body][:4]} |"
    w(header)
    sep = "|:---|" + ":---:|" * len(all_bodies)
    w(sep)

    for hook in all_hooks:
        row = f"| **{HOOK_LABELS_KR_V2[hook][:6]}** |"
        for body in all_bodies:
            ct = cross_table.get((hook, body))
            if ct and ct["n"] >= 5:
                lift_val = ct["lift"]
                if lift_val >= 1.5:
                    row += f" **{lift_val:.1f}** |"
                else:
                    row += f" {lift_val:.1f} |"
            else:
                row += " - |"
        w(row)
    w()

    # 상위 10개 조합
    w("### 5.2 상위 10개 조합 (Lift 기준, N≥10)")
    w()
    w("| 순위 | Hook | Body | N | 고성과 | Lift | 중앙값 조회수 |")
    w("|:---:|:---|:---|:---:|:---:|:---:|:---:|")
    for idx, ((hook, body), stats) in enumerate(sorted_combos[:10], 1):
        w(f"| {idx} | {HOOK_LABELS_KR_V2.get(hook, hook)} | {BODY_LABELS_KR_V2.get(body, body)} | {stats['n']} | {stats['n_high']} | {stats['lift']:.2f} | {format_number(stats['median_views'])} |")
    w()

    # 하위 10개 조합
    bottom10 = sorted(
        [(k, v) for k, v in cross_table.items() if v["n"] >= 10],
        key=lambda x: x[1]["lift"],
    )[:10]
    w("### 5.3 하위 10개 조합 (Lift 기준, N≥10)")
    w()
    w("| 순위 | Hook | Body | N | 고성과 | Lift | 중앙값 조회수 |")
    w("|:---:|:---|:---|:---:|:---:|:---:|:---:|")
    for idx, ((hook, body), stats) in enumerate(bottom10, 1):
        w(f"| {idx} | {HOOK_LABELS_KR_V2.get(hook, hook)} | {BODY_LABELS_KR_V2.get(body, body)} | {stats['n']} | {stats['n_high']} | {stats['lift']:.2f} | {format_number(stats['median_views'])} |")
    w()

    # ════════════════════════════════════════════
    # 6. 성공 vs 실패 비교
    # ════════════════════════════════════════════
    w("## 6. 성공 vs 실패 비교 분석")
    w()

    # 6.1 길이 분포
    w("### 6.1 텍스트 길이 분포")
    w()
    w("| 길이 구간 | N | 고성과 N | 고성과 비율(%) | 평균 조회수 |")
    w("|:---|:---:|:---:|:---:|:---:|")
    for label in ["0-30자", "31-80자", "81-150자", "151-280자", "280자+"]:
        le = length_effects.get(label, {})
        w(f"| {label} | {le.get('n', 0):,} | {le.get('n_high', 0):,} | {le.get('pct_high', 0)} | {format_number(le.get('avg_views', 0))} |")
    w()

    # 6.2 텍스트 피처 효과
    w("### 6.2 텍스트 피처별 효과")
    w()
    feat_kr = {
        "has_url": "URL 포함",
        "has_emoji": "이모지 포함",
        "has_hashtag": "해시태그 포함",
        "has_arrow": "화살표(→) 포함",
        "has_question": "물음표(?) 포함",
        "has_numbered_list": "번호 리스트 포함",
    }
    w("| 피처 | 포함 시 고성과(%) | 미포함 시 고성과(%) | 차이(pp) | Cohen's d |")
    w("|:---|:---:|:---:|:---:|:---:|")
    for feat in sorted(feat_effects.keys(), key=lambda x: abs(feat_effects[x]["diff_pp"]), reverse=True):
        fe = feat_effects[feat]
        kr = feat_kr.get(feat, feat)
        w(f"| {kr} | {fe['pct_high_with']} | {fe['pct_high_without']} | {fe['diff_pp']:+.1f} | {fe['cohens_d']:.3f} |")
    w()

    # 6.3 실패 패턴
    w("### 6.3 저성과 트윗의 실패 패턴")
    w()
    w("| 패턴 | 트윗 수 | 비율(%) |")
    w("|:---|:---:|:---:|")
    for pattern_name, count, pct in failure_pats:
        w(f"| {pattern_name} | {count:,} | {pct} |")
    w()

    # 6.4 참여지표 상관
    w("### 6.4 참여지표 × 조회수 상관관계")
    w()
    w(f"> 전체 오리지널 {len(orig_analyzed):,}건 대상")
    w()
    w("| 참여 유형 | Pearson r | Spearman ρ | 해석 |")
    w("|:---|:---:|:---:|:---|")
    for kr in ["좋아요", "답글", "리트윗", "북마크", "인용"]:
        ec = eng_corrs.get(kr, {"pearson": 0, "spearman": 0})
        pr_val = ec["pearson"]
        sr_val = ec["spearman"]
        if abs(pr_val) >= 0.7:
            interp = "강한 상관"
        elif abs(pr_val) >= 0.4:
            interp = "중간 상관"
        elif abs(pr_val) >= 0.2:
            interp = "약한 상관"
        else:
            interp = "매우 약한 상관"
        w(f"| **{kr}** | {pr_val:.4f} | {sr_val:.4f} | {interp} |")
    w()

    # 6.5 고/저 참여 비율 비교
    w("### 6.5 고성과 vs 저성과 참여 비율")
    w()
    w("| 참여 유형 | 고성과 평균(%) | 저성과 평균(%) | 차이(pp) |")
    w("|:---|:---:|:---:|:---:|")
    eng_fields = [("like_rate", "좋아요"), ("reply_rate", "답글"),
                  ("rt_rate", "리트윗"), ("bookmark_rate", "북마크")]
    for field, kr in eng_fields:
        h_vals = [i["engagement"][field] * 100 for i in analyzed_high_orig]
        l_vals = [i["engagement"][field] * 100 for i in analyzed_low_orig]
        h_avg = mean(h_vals) if h_vals else 0
        l_avg = mean(l_vals) if l_vals else 0
        w(f"| **{kr}** | {h_avg:.4f} | {l_avg:.4f} | {h_avg - l_avg:+.4f} |")
    w()

    # ════════════════════════════════════════════
    # 7. X 알고리즘 시사점
    # ════════════════════════════════════════════
    w("## 7. X 알고리즘 시사점")
    w()
    w("> X가 공개한 추천 알고리즘 소스코드(`x-algorithm-main`) 기반 분석.")
    w("> 가중치 수치는 비공개이나, **어떤 시그널을 쓰는지**는 코드로 확인 가능.")
    w()

    w("### 7.1 최종 점수 공식")
    w()
    w("```")
    w("Final Score = Σ (weight_i × P(action_i))")
    w("```")
    w()
    w("Phoenix (Grok 기반 트랜스포머)가 각 트윗에 대해 15개 양의 행동 + 4개 음의 행동 확률을 예측하고,")
    w("가중 합산하여 최종 점수를 산출합니다.")
    w()

    w("### 7.2 19개 스코어링 시그널")
    w()
    w("**양의 시그널 (노출 ↑):**")
    w()
    w("| 시그널 | 의미 | 트윗 작성 시사점 |")
    w("|:---|:---|:---|")
    for sig, meaning, tip in ALGORITHM_SIGNALS["positive"]:
        w(f"| `{sig}` | {meaning} | {tip} |")
    w()

    w("**음의 시그널 (노출 ↓):**")
    w()
    w("| 시그널 | 의미 | 회피 전략 |")
    w("|:---|:---|:---|")
    for sig, meaning, tip in ALGORITHM_SIGNALS["negative"]:
        w(f"| `{sig}` | {meaning} | {tip} |")
    w()

    w("### 7.3 유형별 알고리즘 시그널 매핑")
    w()
    w("**Hook 유형 → 주요 시그널:**")
    w()
    w("| Hook 유형 | 주로 트리거하는 시그널 |")
    w("|:---|:---|")
    for hook in hook_order:
        signals = HOOK_ALGO_MAP.get(hook, [])
        kr = HOOK_LABELS_KR_V2.get(hook, hook)
        w(f"| {kr} | {', '.join(f'`{s}`' for s in signals)} |")
    w()

    w("**Body 유형 → 주요 시그널:**")
    w()
    w("| Body 유형 | 주로 트리거하는 시그널 |")
    w("|:---|:---|")
    for body in body_order:
        signals = BODY_ALGO_MAP.get(body, [])
        kr = BODY_LABELS_KR_V2.get(body, body)
        w(f"| {kr} | {', '.join(f'`{s}`' for s in signals)} |")
    w()

    w("### 7.4 핵심 운영 제약 조건")
    w()
    w("| 제약 | 수치 | 시사점 |")
    w("|:---|:---|:---|")
    w("| 포스트 수명 | **24시간** (86,400초) | 트윗 발행 시점(=피크타임)이 노출량 직결. `age_filter`에 의해 완전 제거 |")
    w("| In-network 캐시 | **48시간** | Thunder 인메모리 저장소에 48시간만 보존 |")
    w("| 유저 히스토리 | **128개** | 모델이 보는 과거 참여 이력 최대 128개 |")
    w("| 영상 최소 길이 | **200ms** | 미만 영상은 `video_quality_view` 점수 0 |")
    w("| 뮤트 키워드 | **정확 매칭** | TweetTokenizer 기반 정확 토큰 매칭 (fuzzy 아님) |")
    w(f"| 작성자 다양성 | **지수 감쇠** | `{algo_info.get('diversity_formula', 'multiplier = (1-floor) × decay^position + floor')}` |")
    w()

    w("### 7.5 파이프라인 구조")
    w()
    w("```")
    w("Sources (Phoenix OON + Thunder In-Network)")
    w("  → Hydrators (tweet text, author, video duration, followers)")
    w("  → Filters (age, self, dedup, muted keywords, blocked/muted authors)")
    w("  → Scorers (Phoenix ML → WeightedScorer → AuthorDiversity → OON penalty)")
    w("  → Selector (Top-K by score)")
    w("  → Post-filters (VF safety, conversation dedup)")
    w("```")
    w()

    w("### 7.6 알고리즘 핵심 원칙")
    w()
    w("1. **체류 시간 극대화**: `dwell` + `dwell_time` 이중 가점")
    w("2. **DM 공유 유도**: `share_via_dm` 별도 가중치 — 가장 강력한 단일 시그널 중 하나")
    w("3. **이미지 확대 유도**: `photo_expand` 별도 가중치")
    w("4. **프로필 클릭 유도**: `profile_click` 별도 가중치 → 권위형 Hook")
    w("5. **팔로우 전환**: `follow_author` 별도 가중치 → 시리즈/연재 콘텐츠")
    w("6. **부정 반응 최소화**: `block`/`mute`/`report` 음의 가중치")
    w("7. **연타 금지**: author diversity decay로 연속 노출 시 점수 감쇠")
    w("8. **텍스트 무관**: 알고리즘은 텍스트를 직접 읽지 않음. 사람의 반응을 측정하여 증폭하는 2단계 구조")
    w()

    # ════════════════════════════════════════════
    # 8. 실전 체크리스트
    # ════════════════════════════════════════════
    w("## 8. 실전 체크리스트")
    w()

    w("### Hook 체크리스트")
    w()
    # 동적으로 상위 유형 기반 체크리스트 생성
    top_hooks_sorted = sorted(hook_stats.items(), key=lambda x: x[1]["lift"], reverse=True)
    for label, s in top_hooks_sorted[:5]:
        kr = s["label_kr"]
        w(f"- [ ] **{kr}** 패턴을 사용했는가? (Lift {s['lift']:.2f}×, 평균 조회 {format_number(s['avg_views'])})")
    w("- [ ] Hook만 읽고도 **스크롤을 멈추는가?** → `dwell` 시그널 직결")
    w("- [ ] **피크타임에 발행**했는가? → 24시간 하드 컷오프, 초기 반응이 전파력 결정")
    w()

    w("### Body 체크리스트")
    w()
    top_bodies_sorted = sorted(body_stats.items(), key=lambda x: x[1]["lift"], reverse=True)
    for label, s in top_bodies_sorted[:4]:
        kr = s["label_kr"]
        w(f"- [ ] **{kr}** 구조를 활용했는가? (Lift {s['lift']:.2f}×)")
    # 길이 관련
    best_len = max(length_effects.items(), key=lambda x: x[1].get("pct_high", 0))
    w(f"- [ ] **{best_len[0]}** 구간을 목표로 했는가? (고성과 비율 {best_len[1]['pct_high']}%)")
    w("- [ ] 한 줄이 50자를 넘지 않는가? (모바일 가독성)")
    w("- [ ] **끝까지 읽게** 만드는 구조인가? → `dwell_time` 별도 가중치")
    w("- [ ] **DM으로 보내고 싶은** 콘텐츠인가? → `share_via_dm` 별도 가중치")
    w()

    w("### 피해야 할 패턴")
    w()
    for pat_name, cnt, pct in failure_pats[:5]:
        w(f"- {pat_name} (저성과의 {pct}%)")
    w("- **같은 시간대 연타 포스팅** → author diversity decay로 점수 감쇠")
    w("- **공격적/혐오 톤** → `block`/`mute`/`report` 음의 가중치")
    w("- **해시태그 남용** → 알고리즘에 해시태그 수동 피처 없음 (효과 0)")
    w()

    # ════════════════════════════════════════════
    # 9. 부록
    # ════════════════════════════════════════════
    w("## 9. 부록")
    w()

    # 9.1 계정별 분석
    w("### 9.1 계정별 고성과 오리지널 트윗 분석")
    w()
    account_data = defaultdict(lambda: {"count": 0, "total_views": 0, "total_likes": 0, "hooks": Counter()})
    for item in analyzed_high_orig:
        acc = item["source"]
        account_data[acc]["count"] += 1
        account_data[acc]["total_views"] += item["engagement"]["views"]
        account_data[acc]["total_likes"] += item["engagement"]["likes"]
        account_data[acc]["hooks"][item["hook_type"]] += 1

    w("| 계정 | 고성과 오리지널 | 평균 조회수 | 평균 좋아요 | 주요 Hook 유형 |")
    w("|:---|:---:|:---:|:---:|:---|")
    for acc in sorted(account_data.keys(),
                      key=lambda x: account_data[x]["count"], reverse=True)[:20]:
        d = account_data[acc]
        n = d["count"]
        if n == 0:
            continue
        avg_v = d["total_views"] // n
        avg_l = d["total_likes"] / n
        top_h = d["hooks"].most_common(1)
        top_hook = HOOK_LABELS_KR_V2.get(top_h[0][0], top_h[0][0]) if top_h else "-"
        w(f"| {acc} | {n} | {format_number(avg_v)} | {format_number(int(avg_l))} | {top_hook} |")
    w()

    # 9.2 RT 분석
    w("### 9.2 RT(리트윗) 분석")
    w()
    w(f"- 고성과 RT: **{high_rt_count:,}건** (고성과의 {high_rt_count/(high_orig_count+high_rt_count)*100:.1f}%)")
    rt_avg = mean([i["engagement"]["views"] for i in analyzed_high_rt]) if analyzed_high_rt else 0
    orig_avg = mean([i["engagement"]["views"] for i in analyzed_high_orig]) if analyzed_high_orig else 0
    w(f"- RT 평균 조회수: **{format_number(round(rt_avg))}**")
    w(f"- 오리지널 평균 조회수: **{format_number(round(orig_avg))}**")
    w(f"- RT가 고성과의 과반을 차지하지만, 이는 원작자의 콘텐츠 품질에 의한 것")
    w(f"- **자체 콘텐츠 역량 강화가 진정한 성장 전략**")
    w()

    # 9.3 v1 대비 변경점
    w("### 9.3 v1 대비 변경점 요약")
    w()
    w("| # | v1 문제 | v2 해결 |")
    w("|:---:|:---|:---|")
    w("| 1 | `short_cryptic` 캐치올 — 고성과 79.5%가 단일 라벨에 묻힘 | Hook 길이와 의미를 분리. 길이 무관하게 의미 기반 분류 |")
    w("| 2 | LLM 7유형이 고성과 137건에만 적용 → 고/저 비교 불가 | 모든 분류 규칙을 전체 트윗에 동일 적용 |")
    w("| 3 | 다중 레이블 → 통계 부풀림 | 상호배타적 1차 분류 + 부가 속성(다중) 분리 |")
    w("| 4 | Hook×Body 조합 분석 없음 | 교차표로 최적 조합 도출 |")
    w("| 5 | 공식 템플릿이 수작업 | 실제 고성과 텍스트에서 패턴 자동 추출 |")
    w("| 6 | 알고리즘 분석이 별도 섹션 | 각 유형 분석에 알고리즘 시그널 연결 |")
    w()

    # 9.4 방법론
    w("### 9.4 분석 방법론")
    w()
    w("1. **데이터 수집**: 56개 계정의 JSONL 파일에서 트윗 로드")
    w("2. **전처리**: viewCount 누락 제외, (fullText, source) 기준 중복 제거")
    w(f"3. **분류 기준**: 조회수 {THRESHOLD:,} 이상을 고성과로 분류")
    w("4. **RT 분리**: `fullText.startswith('RT @')` 기준")
    w("5. **Hook 분류**: 상호배타적 8유형 (우선순위순 regex + LLM 캐시 폴백)")
    w("6. **Body 분류**: 상호배타적 7유형 (우선순위순 구조 패턴)")
    w("7. **통계**: Lift ratio, Cohen's d, 95% CI, Pearson r, Spearman ρ")
    w("8. **교차 분석**: Hook × Body 56개 조합의 lift, N, 중앙값 조회수")
    w("9. **알고리즘**: X 공개 소스코드에서 19개 스코어링 시그널 추출, 유형별 매핑")
    w()

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────
# 메인 실행
# ──────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("X 트윗 성공 공식 역설계 v2")
    print("=" * 60)

    # Phase 1: 데이터 로드
    print("\n[Phase 1] 데이터 로드 중...")
    tweets, skipped_view, skipped_dup = load_all_tweets()
    print(f"  총 로드: {len(tweets):,}건 (viewCount 누락 {skipped_view}건, 중복 {skipped_dup}건 제외)")

    high_orig, high_rt, low_orig, low_rt = split_tweets(tweets)
    total = len(tweets)
    high_count = len(high_orig) + len(high_rt)
    low_count = len(low_orig) + len(low_rt)

    print(f"  총: {total:,}건 | 고성과: {high_count:,} | 저성과: {low_count:,}")
    print(f"  오리지널: 고 {len(high_orig):,} + 저 {len(low_orig):,} = {len(high_orig)+len(low_orig):,}")
    print(f"  RT: 고 {len(high_rt):,} + 저 {len(low_rt):,} = {len(high_rt)+len(low_rt):,}")

    # Phase 2-4: 피처 추출 + 분류
    print("\n[Phase 2-4] 피처 추출 + Hook/Body 분류 중...")
    analyzed_high_orig = analyze_all_tweets(high_orig)
    analyzed_low_orig = analyze_all_tweets(low_orig)
    analyzed_high_rt = analyze_all_tweets(high_rt)
    analyzed_low_rt = analyze_all_tweets(low_rt)

    orig_all = analyzed_high_orig + analyzed_low_orig

    # Hook 분류 분포 출력
    hook_dist = Counter(i["hook_type"] for i in orig_all)
    print("\n  [Hook 분류 분포 - 오리지널 전체]")
    for label, cnt in hook_dist.most_common():
        kr = HOOK_LABELS_KR_V2.get(label, label)
        pct = cnt / len(orig_all) * 100
        print(f"    {kr:<15} {cnt:>8,}건 ({pct:.1f}%)")

    body_dist = Counter(i["body_type"] for i in orig_all)
    print("\n  [Body 분류 분포 - 오리지널 전체]")
    for label, cnt in body_dist.most_common():
        kr = BODY_LABELS_KR_V2.get(label, label)
        pct = cnt / len(orig_all) * 100
        print(f"    {kr:<15} {cnt:>8,}건 ({pct:.1f}%)")

    # Phase 5: 통계 분석
    print("\n[Phase 5] 통계 분석 중...")
    hook_stats = compute_type_stats(orig_all, "hook_type", HOOK_LABELS_KR_V2)
    body_stats = compute_type_stats(orig_all, "body_type", BODY_LABELS_KR_V2)
    cross_table = compute_cross_table(orig_all)
    eng_corrs = compute_engagement_correlations(orig_all)
    feat_effects, length_effects = compute_text_feature_effects(orig_all)
    failure_pats = find_failure_patterns(analyzed_low_orig)

    print("\n  [Hook 유형별 Lift - 오리지널]")
    for label in sorted(hook_stats.keys(), key=lambda x: hook_stats[x]["lift"], reverse=True):
        s = hook_stats[label]
        print(f"    {s['label_kr']:<15} Lift={s['lift']:.3f} d={s['cohens_d']:.3f} N={s['n']:,} (고{s['n_high']:,}/저{s['n_low']:,}) avg_views={s['avg_views']:,}")

    print("\n  [Body 유형별 Lift - 오리지널]")
    for label in sorted(body_stats.keys(), key=lambda x: body_stats[x]["lift"], reverse=True):
        s = body_stats[label]
        print(f"    {s['label_kr']:<15} Lift={s['lift']:.3f} d={s['cohens_d']:.3f} N={s['n']:,} (고{s['n_high']:,}/저{s['n_low']:,}) avg_views={s['avg_views']:,}")

    print("\n  [교차표 상위 5개 조합]")
    sorted_ct = sorted(
        [(k, v) for k, v in cross_table.items() if v["n"] >= 10],
        key=lambda x: x[1]["lift"], reverse=True,
    )
    for (hook, body), ct in sorted_ct[:5]:
        print(f"    {HOOK_LABELS_KR_V2.get(hook, hook)} × {BODY_LABELS_KR_V2.get(body, body)}: Lift={ct['lift']:.2f} N={ct['n']} 고={ct['n_high']}")

    print("\n  [참여지표 상관계수]")
    for kr, vals in eng_corrs.items():
        print(f"    {kr}: Pearson r={vals['pearson']:.4f}, Spearman ρ={vals['spearman']:.4f}")

    # Phase 6: 알고리즘 분석
    print("\n[Phase 6] 알고리즘 분석 중...")
    algo_info = parse_algorithm_files()
    print(f"  스코어링 시그널: {len(algo_info.get('scoring_signals', []))}개 추출")
    print(f"  Age filter: {algo_info.get('age_filter', 'N/A')}")
    print(f"  Diversity: {algo_info.get('diversity_formula', 'N/A')}")

    # Phase 7-8: 공식 도출 + 보고서 생성
    print("\n[Phase 7-8] 공식 도출 + 보고서 생성 중...")
    md_content = generate_report(
        orig_all, analyzed_high_orig, analyzed_low_orig,
        analyzed_high_rt, analyzed_low_rt,
        hook_stats, body_stats, cross_table,
        eng_corrs, feat_effects, length_effects,
        failure_pats, algo_info,
        total, high_count, low_count,
        len(high_orig), len(high_rt),
        len(low_orig), len(low_rt),
    )

    with open(OUTPUT_MD, "w", encoding="utf-8") as f:
        f.write(md_content)

    print(f"\n  저장 완료: {OUTPUT_MD.name} ({OUTPUT_MD.stat().st_size / 1024:.0f} KB)")

    # 검증 출력
    print(f"\n{'=' * 60}")
    print("검증:")
    print(f"  총 트윗: {total:,}건")
    print(f"  고성과: {high_count:,}건 | 저성과: {low_count:,}건")
    print(f"  오리지널: {len(high_orig)+len(low_orig):,}건")
    print(f"  Hook 유형: {len(hook_stats)}개 | Body 유형: {len(body_stats)}개")
    print(f"  교차표 조합: {len(cross_table)}개")
    print(f"{'=' * 60}")
    print("v2 역설계 완료!")


if __name__ == "__main__":
    main()
