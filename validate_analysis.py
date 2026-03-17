"""
트윗 분석 전면 점검 — 상관관계 검증 & 우선순위 분류

기존 reverse_engineer_tweets.py의 규칙/공식을
실제 데이터 상관관계 + X 알고리즘 소스코드와 교차 검증.

산출물: validation_report.md
"""

import json
import re
import math
import sys
import io
from pathlib import Path
from collections import defaultdict

# Windows cp949 인코딩 문제 방지
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "x 자료"
LLM_CACHE_FILE = BASE_DIR / "llm_hook_cache.json"
OUTPUT_MD = BASE_DIR / "validation_report.md"
THRESHOLD = 100_000

# LLM 캐시 로드
_LLM_CACHE = {}
if LLM_CACHE_FILE.exists():
    with open(LLM_CACHE_FILE, "r", encoding="utf-8") as _f:
        _LLM_CACHE = json.load(_f)
LLM_HOOK_OVERRIDES = _LLM_CACHE.get("hook_type_overrides", {})

URL_RE = re.compile(r'https?://\S+')

# LLM 캐시에서만 분류된 유형 (고성과 137건만 존재, 저성과 비교 불가)
LLM_ONLY_TYPES = {"narrative_situation", "info_fact", "opinion_reaction",
                  "celeb_mention", "promotion", "emotion_empathy", "comparison_choice"}

# ─────────────────────────────────────────────
# Step 1. 데이터 로드 (reverse_engineer_tweets.py 로직 복사)
# ─────────────────────────────────────────────

def load_all_tweets():
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
# Step 2. Hook/Body 분류 (reverse_engineer_tweets.py 로직 복사)
# ─────────────────────────────────────────────

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
    "bracket_title": re.compile(r'(\[.+\]|【.+】|「.+」|『.+』)'),
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
    "short_cryptic": None,
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
    "narrative_situation": "서사/상황형",
    "info_fact": "정보전달형",
    "opinion_reaction": "의견/반응형",
    "celeb_mention": "셀럽/유명인형",
    "promotion": "홍보/추천형",
    "emotion_empathy": "감정/공감형",
    "comparison_choice": "비교/선택형",
}

BODY_LABELS_KR = {
    "numbered_list": "번호 리스트형",
    "arrow_flow": "화살표 흐름형",
    "short_line_stacking": "짧은 줄 쌓기형",
    "narrative_story": "내러티브/서사형",
    "media_post": "미디어 의존형",
}


def extract_hook(text):
    text_clean = URL_RE.sub("", text).strip()
    for line in text_clean.split("\n"):
        line = line.strip()
        if line:
            return line
    return text_clean


def classify_hook(hook_text):
    labels = []
    for name, pattern in HOOK_PATTERNS.items():
        if name == "short_cryptic":
            if len(hook_text) <= 25:
                labels.append(name)
        elif pattern and pattern.search(hook_text):
            labels.append(name)
    if not labels:
        llm_label = LLM_HOOK_OVERRIDES.get(hook_text)
        if llm_label:
            labels.append(llm_label)
        else:
            labels.append("unclassified")
    return labels


def classify_body(text):
    text_clean = URL_RE.sub("", text).strip()
    labels = []
    if len(re.findall(r'\n\s*\d+[.)]\s', text_clean)) >= 2:
        labels.append("numbered_list")
    elif len(re.findall(r'[\n]\s*[①②③④⑤⑥⑦⑧⑨⑩❶❷❸❹❺❻❼❽❾❿]', text_clean)) >= 2:
        labels.append("numbered_list")
    elif len(re.findall(r'\d️⃣', text_clean)) >= 2:
        labels.append("numbered_list")
    if '→' in text_clean or '➡' in text_clean or '▶' in text_clean:
        labels.append("arrow_flow")
    lines = [l.strip() for l in text_clean.split("\n") if l.strip()]
    if len(lines) >= 4 and all(len(l) < 50 for l in lines):
        labels.append("short_line_stacking")
    if len(text_clean) > 120 and "numbered_list" not in labels:
        labels.append("narrative_story")
    if len(text_clean) < 60:
        labels.append("media_post")
    if not labels:
        labels.append("unclassified")
    return labels


# ─────────────────────────────────────────────
# Step 2b. 피처 추출
# ─────────────────────────────────────────────

EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002702-\U000027B0"
    "\U0001f900-\U0001f9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF"
    "]+",
    flags=re.UNICODE
)
HASHTAG_RE = re.compile(r'#\S+')
MENTION_RE = re.compile(r'@\w+')
QUESTION_RE = re.compile(r'\?')
NUMBER_LIST_RE = re.compile(r'\n\s*\d+[.)]\s')
ARROW_RE = re.compile(r'[→➡▶]')


def extract_features(tweet):
    text = tweet.get("fullText", "")
    text_clean = URL_RE.sub("", text).strip()
    views = tweet.get("viewCount", 0) or 0
    likes = tweet.get("likeCount", 0) or 0
    retweets = tweet.get("retweetCount", 0) or 0
    replies = tweet.get("replyCount", 0) or 0
    bookmarks = tweet.get("bookmarkCount", 0) or 0
    quotes = tweet.get("quoteCount", 0) or 0

    # 참여율
    like_rate = likes / views if views > 0 else 0
    retweet_rate = retweets / views if views > 0 else 0
    reply_rate = replies / views if views > 0 else 0
    bookmark_rate = bookmarks / views if views > 0 else 0
    quote_rate = quotes / views if views > 0 else 0
    total_er = (likes + retweets + replies + bookmarks + quotes) / views if views > 0 else 0

    # 텍스트
    text_length = len(text_clean)
    word_count = len(text_clean.split())
    line_count = len([l for l in text_clean.split("\n") if l.strip()])
    if text_length <= 30:
        length_bin = "0-30"
    elif text_length <= 80:
        length_bin = "31-80"
    elif text_length <= 150:
        length_bin = "81-150"
    elif text_length <= 280:
        length_bin = "151-280"
    else:
        length_bin = "280+"

    # 텍스트 요소
    has_url = 1 if URL_RE.search(text) else 0
    hashtag_count = len(HASHTAG_RE.findall(text))
    has_hashtag = 1 if hashtag_count > 0 else 0
    has_emoji = 1 if EMOJI_RE.search(text) else 0
    has_mention = 1 if MENTION_RE.search(text) else 0
    has_question = 1 if QUESTION_RE.search(text_clean) else 0
    has_number_list = 1 if len(NUMBER_LIST_RE.findall(text_clean)) >= 2 else 0
    has_arrow = 1 if ARROW_RE.search(text_clean) else 0

    # 분류
    hook_text = extract_hook(text)
    hook_labels = classify_hook(hook_text)
    body_labels = classify_body(text)
    is_rt = 1 if text.startswith("RT @") else 0

    return {
        "text": text,
        "hook_text": hook_text,
        "source": tweet.get("source", ""),
        # 원시 지표
        "viewCount": views,
        "likeCount": likes,
        "retweetCount": retweets,
        "replyCount": replies,
        "bookmarkCount": bookmarks,
        "quoteCount": quotes,
        # 참여율
        "like_rate": like_rate,
        "retweet_rate": retweet_rate,
        "reply_rate": reply_rate,
        "bookmark_rate": bookmark_rate,
        "quote_rate": quote_rate,
        "total_er": total_er,
        # 텍스트
        "text_length": text_length,
        "word_count": word_count,
        "line_count": line_count,
        "length_bin": length_bin,
        # 텍스트 요소
        "has_url": has_url,
        "hashtag_count": hashtag_count,
        "has_hashtag": has_hashtag,
        "has_emoji": has_emoji,
        "has_mention": has_mention,
        "has_question": has_question,
        "has_number_list": has_number_list,
        "has_arrow": has_arrow,
        # 분류
        "hook_labels": hook_labels,
        "body_labels": body_labels,
        "is_rt": is_rt,
    }


# ─────────────────────────────────────────────
# Step 3. 통계 도구 (stdlib only)
# ─────────────────────────────────────────────

def pearson_r(xs, ys):
    n = len(xs)
    if n < 3:
        return (0.0, 1.0)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return (0.0, 1.0)
    r = num / (den_x * den_y)
    r = max(-1.0, min(1.0, r))
    # t-검정 기반 p-value (양측)
    if abs(r) >= 1.0:
        p_val = 0.0
    else:
        t_stat = r * math.sqrt((n - 2) / (1 - r * r))
        df = n - 2
        p_val = _t_to_p(abs(t_stat), df)
    return (round(r, 4), p_val)


def spearman_rank(xs, ys):
    n = len(xs)
    if n < 3:
        return (0.0, 1.0)

    def rank_data(data):
        indexed = sorted(enumerate(data), key=lambda x: x[1])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j < n - 1 and indexed[j + 1][1] == indexed[j][1]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                ranks[indexed[k][0]] = avg_rank
            i = j + 1
        return ranks

    rank_x = rank_data(xs)
    rank_y = rank_data(ys)
    return pearson_r(rank_x, rank_y)


def cohens_d(group1, group2):
    n1, n2 = len(group1), len(group2)
    if n1 < 2 or n2 < 2:
        return 0.0
    mean1 = sum(group1) / n1
    mean2 = sum(group2) / n2
    var1 = sum((x - mean1) ** 2 for x in group1) / (n1 - 1)
    var2 = sum((x - mean2) ** 2 for x in group2) / (n2 - 1)
    pooled_std = math.sqrt(((n1 - 1) * var1 + (n2 - 1) * var2) / (n1 + n2 - 2))
    if pooled_std == 0:
        return 0.0
    return (mean1 - mean2) / pooled_std


def percentiles(values, quantiles=(10, 25, 50, 75, 90, 95, 99)):
    if not values:
        return {q: 0 for q in quantiles}
    s = sorted(values)
    n = len(s)
    result = {}
    for q in quantiles:
        k = (q / 100) * (n - 1)
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            result[q] = s[int(k)]
        else:
            result[q] = s[f] + (k - f) * (s[c] - s[f])
    return result


def _t_to_p(t_stat, df):
    """근사 양측 p-value (t-분포 → 정규 근사, df ≥ 30이면 충분히 정확)."""
    if df <= 0:
        return 1.0
    if df >= 30:
        # 정규 근사
        return 2 * _normal_sf(t_stat)
    # 소표본: Welch-Satterthwaite 근사 대신 보수적 정규 근사 사용
    return 2 * _normal_sf(t_stat)


def _normal_sf(x):
    """표준 정규 분포의 survival function (1 - CDF) 근사."""
    # Abramowitz & Stegun 근사 (7.1.26)
    if x < 0:
        return 1.0 - _normal_sf(-x)
    t = 1.0 / (1.0 + 0.2316419 * x)
    d = 0.3989422804014327  # 1/sqrt(2*pi)
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    return d * math.exp(-0.5 * x * x) * poly


def mean_ci_95(values):
    """95% 신뢰구간 (평균 ± 1.96*SE)."""
    n = len(values)
    if n < 2:
        m = values[0] if values else 0
        return m, m, m
    m = sum(values) / n
    var = sum((x - m) ** 2 for x in values) / (n - 1)
    se = math.sqrt(var / n)
    return m, m - 1.96 * se, m + 1.96 * se


# ─────────────────────────────────────────────
# Step 4. 핵심 분석 (6가지)
# ─────────────────────────────────────────────

def analysis_4a(features):
    """참여지표 vs viewCount 상관관계."""
    views = [f["viewCount"] for f in features]
    fields = [
        ("likeCount", "좋아요"),
        ("retweetCount", "리트윗"),
        ("replyCount", "답글"),
        ("bookmarkCount", "북마크"),
        ("quoteCount", "인용"),
    ]
    results = []
    for field, kr in fields:
        vals = [f[field] for f in features]
        pr, pp = pearson_r(vals, views)
        sr, sp = spearman_rank(vals, views)
        results.append({
            "field": field,
            "name_kr": kr,
            "pearson_r": pr,
            "pearson_p": pp,
            "spearman_rho": sr,
            "spearman_p": sp,
        })
    return results


def analysis_4b(features, high_features, low_features):
    """텍스트 피처 vs viewCount 상관관계 + Cohen's d."""
    views = [f["viewCount"] for f in features]
    text_fields = [
        ("text_length", "텍스트 길이"),
        ("line_count", "줄 수"),
        ("word_count", "단어 수"),
        ("has_url", "URL 포함"),
        ("hashtag_count", "해시태그 수"),
        ("has_emoji", "이모지 포함"),
        ("has_question", "물음표 포함"),
        ("has_number_list", "번호 리스트"),
        ("has_arrow", "화살표 포함"),
    ]
    results = []
    for field, kr in text_fields:
        vals = [f[field] for f in features]
        pr, pp = pearson_r(vals, views)
        sr, sp = spearman_rank(vals, views)
        h_vals = [f[field] for f in high_features]
        l_vals = [f[field] for f in low_features]
        d = cohens_d(h_vals, l_vals)
        results.append({
            "field": field,
            "name_kr": kr,
            "pearson_r": pr,
            "pearson_p": pp,
            "spearman_rho": sr,
            "spearman_p": sp,
            "cohens_d": round(d, 4),
        })
    return results


def analysis_4c(features):
    """참여지표 간 교차 상관 매트릭스."""
    fields = ["likeCount", "retweetCount", "replyCount", "bookmarkCount", "quoteCount"]
    names = ["좋아요", "리트윗", "답글", "북마크", "인용"]
    matrix = {}
    for i, (fi, ni) in enumerate(zip(fields, names)):
        for j, (fj, nj) in enumerate(zip(fields, names)):
            if i <= j:
                vals_i = [f[fi] for f in features]
                vals_j = [f[fj] for f in features]
                r, p = pearson_r(vals_i, vals_j)
                matrix[(ni, nj)] = (r, p)
    return matrix, names


def analysis_4d(high_features):
    """분포 분석 — 고성과 그룹 내 백분위수."""
    fields = [
        ("viewCount", "조회수"),
        ("likeCount", "좋아요"),
        ("retweetCount", "리트윗"),
        ("bookmarkCount", "북마크"),
        ("total_er_pct", "총 ER(%)"),
    ]
    # total_er를 백분율로 변환한 임시 필드 추가
    for f in high_features:
        f["total_er_pct"] = f["total_er"] * 100
    results = {}
    for field, kr in fields:
        vals = [f[field] for f in high_features]
        pcts = percentiles(vals)
        m = sum(vals) / len(vals) if vals else 0
        median = pcts.get(50, 0)
        skew_indicator = "우측 편향 (소수 극고치)" if m > median * 1.3 else "비교적 대칭"
        results[kr] = {
            "percentiles": pcts,
            "mean": m,
            "skew": skew_indicator,
        }
    return results


def analysis_4e(features, high_features, low_features):
    """Hook/Body 유형별 효과 검증."""
    # Hook 유형별
    hook_groups_high = defaultdict(list)
    hook_groups_low = defaultdict(list)
    hook_groups = defaultdict(list)
    for f in features:
        for label in f["hook_labels"]:
            hook_groups[label].append(f["viewCount"])
            if f["viewCount"] >= THRESHOLD:
                hook_groups_high[label].append(f["viewCount"])
            else:
                hook_groups_low[label].append(f["viewCount"])

    hook_results = {}
    all_views = [f["viewCount"] for f in features]
    overall_mean = sum(all_views) / len(all_views)

    for label, views in hook_groups.items():
        if label == "unclassified":
            continue
        n = len(views)
        if n < 5:
            continue
        m, ci_lo, ci_hi = mean_ci_95(views)
        other = [v for f in features for v in [f["viewCount"]]
                 if label not in f["hook_labels"]]
        d = cohens_d(views, other)
        n_low = len(hook_groups_low.get(label, []))
        is_llm_only = label in LLM_ONLY_TYPES and n_low == 0
        hook_results[label] = {
            "count": n,
            "count_high": len(hook_groups_high.get(label, [])),
            "count_low": n_low,
            "mean_views": round(m),
            "ci_low": round(ci_lo),
            "ci_high": round(ci_hi),
            "cohens_d": round(d, 4),
            "bias_note": "⚠️ 고성과만 (LLM 캐시)" if is_llm_only else "",
        }

    # Body 유형별
    body_groups = defaultdict(list)
    for f in features:
        for label in f["body_labels"]:
            body_groups[label].append(f["viewCount"])

    body_results = {}
    for label, views in body_groups.items():
        if label == "unclassified":
            continue
        n = len(views)
        if n < 5:
            continue
        m, ci_lo, ci_hi = mean_ci_95(views)
        other = [v for f in features for v in [f["viewCount"]]
                 if label not in f["body_labels"]]
        d = cohens_d(views, other)
        body_results[label] = {
            "count": n,
            "mean_views": round(m),
            "ci_low": round(ci_lo),
            "ci_high": round(ci_hi),
            "cohens_d": round(d, 4),
        }

    hook_unclassified = len(hook_groups.get("unclassified", []))
    body_unclassified = len(body_groups.get("unclassified", []))

    return hook_results, body_results, hook_unclassified, body_unclassified


def analysis_4f(features):
    """텍스트 피처 조합 분석."""
    combos = [
        ("has_url AND 151-280자", lambda f: f["has_url"] == 1 and f["length_bin"] == "151-280"),
        ("has_number_list AND short_line_stacking",
         lambda f: f["has_number_list"] == 1 and "short_line_stacking" in f["body_labels"]),
        ("has_arrow AND 151-280자",
         lambda f: f["has_arrow"] == 1 and f["length_bin"] == "151-280"),
        ("has_url AND has_emoji",
         lambda f: f["has_url"] == 1 and f["has_emoji"] == 1),
        ("has_question AND short_cryptic",
         lambda f: f["has_question"] == 1 and "short_cryptic" in f["hook_labels"]),
    ]
    all_views = [f["viewCount"] for f in features]
    results = []
    for name, pred in combos:
        match_views = [f["viewCount"] for f in features if pred(f)]
        non_match_views = [f["viewCount"] for f in features if not pred(f)]
        if len(match_views) < 5:
            continue
        m_match = sum(match_views) / len(match_views)
        m_non = sum(non_match_views) / len(non_match_views) if non_match_views else 0
        d = cohens_d(match_views, non_match_views)
        results.append({
            "combo": name,
            "count": len(match_views),
            "mean_views_match": round(m_match),
            "mean_views_non": round(m_non),
            "cohens_d": round(d, 4),
        })
    return results


# ─────────────────────────────────────────────
# Step 5. X 알고리즘 시그널 매핑
# ─────────────────────────────────────────────

ALGORITHM_SIGNALS = [
    ("favorite_score", "likeCount", "직접 측정", "좋아요"),
    ("reply_score", "replyCount", "직접 측정", "답글"),
    ("retweet_score", "retweetCount", "직접 측정", "리트윗"),
    ("quote_score", "quoteCount", "직접 측정", "인용"),
    ("share_score", "bookmarkCount", "프록시", "공유 (북마크 프록시)"),
    ("dwell_score", "text_length", "프록시", "머무름 (텍스트 길이 프록시)"),
    ("dwell_time", "line_count", "프록시", "체류 시간 (줄 수 프록시)"),
    ("photo_expand_score", "has_url", "프록시", "이미지 확대 (URL 프록시)"),
    ("share_via_dm_score", None, "측정 불가", "DM 공유"),
    ("share_via_copy_link_score", None, "측정 불가", "링크 복사 공유"),
    ("click_score", None, "측정 불가", "트윗 클릭"),
    ("profile_click_score", None, "측정 불가", "프로필 클릭"),
    ("follow_author_score", None, "측정 불가", "팔로우"),
    ("vqv_score", None, "측정 불가", "영상 시청"),
    ("quoted_click_score", None, "측정 불가", "인용 트윗 클릭"),
    ("not_interested_score", None, "측정 불가 (부정)", "관심 없음"),
    ("block_author_score", None, "측정 불가 (부정)", "차단"),
    ("mute_author_score", None, "측정 불가 (부정)", "뮤트"),
    ("report_score", None, "측정 불가 (부정)", "신고"),
]


def analysis_5(features):
    """알고리즘 시그널 매핑 — 측정 가능한 시그널에 대해 상관관계 계산."""
    views = [f["viewCount"] for f in features]
    results = []
    for sig_name, data_field, measurability, description in ALGORITHM_SIGNALS:
        if data_field is None:
            results.append({
                "signal": sig_name,
                "description": description,
                "measurability": measurability,
                "pearson_r": None,
                "spearman_rho": None,
            })
        else:
            vals = [f[data_field] for f in features]
            pr, _ = pearson_r(vals, views)
            sr, _ = spearman_rank(vals, views)
            results.append({
                "signal": sig_name,
                "description": description,
                "measurability": measurability,
                "data_field": data_field,
                "pearson_r": pr,
                "spearman_rho": sr,
            })
    return results


# ─────────────────────────────────────────────
# Step 6. 규칙 검증 엔진
# ─────────────────────────────────────────────

def validate_rules(features, high_features, low_features,
                   engagement_corr, text_corr, hook_effects, body_effects):
    """tweet_formulas.md의 주요 규칙들을 하나씩 판정."""
    rules = []

    # 규칙 1: 좋아요 상관 r=0.6716 (최강)
    like_result = next((r for r in engagement_corr if r["field"] == "likeCount"), None)
    if like_result:
        measured_r = like_result["pearson_r"]
        claimed_r = 0.6716
        diff = abs(measured_r - claimed_r)
        if diff < 0.01:
            verdict = "CONFIRMED"
        elif diff < 0.05:
            verdict = "CONFIRMED (미세 차이)"
        else:
            verdict = "CONTRADICTED"
        is_strongest = all(
            abs(like_result["pearson_r"]) >= abs(r["pearson_r"])
            for r in engagement_corr
        )
        rules.append({
            "rule": f"좋아요 상관 r=0.6716 (최강)",
            "method": "Pearson 재계산",
            "measured": f"r={measured_r}, 최강={'예' if is_strongest else '아니오'}",
            "verdict": verdict,
            "detail": f"기존 {claimed_r} vs 재측정 {measured_r}, 차이={diff:.4f}",
        })

    # 규칙 2: 151-280자 최적
    bins_high = defaultdict(list)
    bins_low = defaultdict(list)
    for f in high_features:
        bins_high[f["length_bin"]].append(f["viewCount"])
    for f in low_features:
        bins_low[f["length_bin"]].append(f["viewCount"])

    all_bins = defaultdict(list)
    for f in features:
        all_bins[f["length_bin"]].append(f["viewCount"])

    bin_means = {}
    for b, vs in all_bins.items():
        bin_means[b] = sum(vs) / len(vs) if vs else 0

    high_pct_151_280 = len(bins_high.get("151-280", [])) / len(high_features) * 100 if high_features else 0
    low_pct_151_280 = len(bins_low.get("151-280", [])) / len(low_features) * 100 if low_features else 0
    diff_pp = high_pct_151_280 - low_pct_151_280
    best_bin = max(bin_means, key=bin_means.get) if bin_means else "N/A"

    if diff_pp > 10 and best_bin == "151-280":
        verdict = "CONFIRMED"
    elif diff_pp > 5:
        verdict = "CONFIRMED (부분)"
    else:
        verdict = "CONTRADICTED"

    rules.append({
        "rule": "151-280자 최적 구간",
        "method": "length_bin별 평균 viewCount + 고/저 비율 차이",
        "measured": f"고성과 {high_pct_151_280:.1f}% vs 저성과 {low_pct_151_280:.1f}% (차이 {diff_pp:+.1f}pp), 최고평균구간={best_bin}",
        "verdict": verdict,
        "detail": f"구간별 평균: {', '.join(f'{b}={round(v):,}' for b, v in sorted(bin_means.items()))}",
    })

    # 규칙 3: 짧은 Hook(≤25자) 79.5%
    short_count = sum(1 for f in high_features if "short_cryptic" in f["hook_labels"])
    short_pct = short_count / len(high_features) * 100 if high_features else 0
    claimed_pct = 79.5
    diff = abs(short_pct - claimed_pct)
    verdict = "CONFIRMED" if diff < 2.0 else ("CONFIRMED (근사)" if diff < 5.0 else "CONTRADICTED")
    rules.append({
        "rule": "짧은 Hook(≤25자) 고성과 비율 79.5%",
        "method": "재집계",
        "measured": f"{short_pct:.1f}%",
        "verdict": verdict,
        "detail": f"기존 79.5% vs 재측정 {short_pct:.1f}%, 차이={diff:.1f}pp",
    })

    # 규칙 4: 화살표 흐름형 최고 ER
    arrow_data = body_effects.get("arrow_flow")
    if arrow_data:
        # ER 비교
        arrow_ers = [f["total_er"] for f in features if "arrow_flow" in f["body_labels"]]
        avg_arrow_er = sum(arrow_ers) / len(arrow_ers) if arrow_ers else 0
        other_body_ers = {}
        for label in BODY_LABELS_KR:
            es = [f["total_er"] for f in features if label in f["body_labels"]]
            if es:
                other_body_ers[label] = sum(es) / len(es)
        is_top_er = all(avg_arrow_er >= v for v in other_body_ers.values())
        verdict = "CONFIRMED" if is_top_er else "CONTRADICTED"
        rules.append({
            "rule": "화살표 흐름형 최고 ER",
            "method": "body_type별 ER 비교",
            "measured": f"arrow_flow ER={avg_arrow_er*100:.3f}%, 최고={'예' if is_top_er else '아니오'}",
            "verdict": verdict,
            "detail": f"Body별 ER: {', '.join(f'{BODY_LABELS_KR.get(k,k)}={v*100:.3f}%' for k, v in sorted(other_body_ers.items(), key=lambda x: x[1], reverse=True))}",
        })

    # 규칙 5: URL 포함 76.8% vs 73.9%
    high_url_count = sum(1 for f in high_features if f["has_url"] == 1)
    low_url_count = sum(1 for f in low_features if f["has_url"] == 1)
    high_url_pct = high_url_count / len(high_features) * 100 if high_features else 0
    low_url_pct = low_url_count / len(low_features) * 100 if low_features else 0
    claimed_high = 76.8
    claimed_low = 73.9
    diff_h = abs(high_url_pct - claimed_high)
    diff_l = abs(low_url_pct - claimed_low)
    verdict = "CONFIRMED" if diff_h < 2 and diff_l < 2 else "CONTRADICTED"
    url_d = cohens_d(
        [f["viewCount"] for f in features if f["has_url"] == 1],
        [f["viewCount"] for f in features if f["has_url"] == 0]
    )
    rules.append({
        "rule": "URL 포함 비율 고성과 76.8% vs 저성과 73.9%",
        "method": "비율 재계산 + Cohen's d",
        "measured": f"고성과 {high_url_pct:.1f}% vs 저성과 {low_url_pct:.1f}%, Cohen's d={url_d:.4f}",
        "verdict": verdict,
        "detail": f"기존 76.8/73.9 vs 재측정 {high_url_pct:.1f}/{low_url_pct:.1f}",
    })

    # 규칙 6: 해시태그 무효과
    hashtag_result = next((r for r in text_corr if r["field"] == "hashtag_count"), None)
    if hashtag_result:
        r_val = hashtag_result["pearson_r"]
        d_val = hashtag_result["cohens_d"]
        verdict = "CONFIRMED" if abs(r_val) < 0.05 and abs(d_val) < 0.2 else "CONTRADICTED"
        rules.append({
            "rule": "해시태그 무효과",
            "method": "hashtag_count vs viewCount",
            "measured": f"Pearson r={r_val}, Cohen's d={d_val}",
            "verdict": verdict,
            "detail": f"|r|<0.05 AND |d|<0.2이면 무효과",
        })

    # 규칙 7: 인용트윗 dominant = 최고 조회
    dom_stats = defaultdict(list)
    for f in features:
        v = f["viewCount"]
        if v == 0:
            continue
        best_field = None
        best_ratio = -1
        for field in ["likeCount", "retweetCount", "replyCount", "bookmarkCount", "quoteCount"]:
            ratio = f[field] / v
            if ratio > best_ratio:
                best_ratio = ratio
                best_field = field
        if best_field:
            dom_stats[best_field].append(v)
    dom_means = {}
    for field, vs in dom_stats.items():
        dom_means[field] = sum(vs) / len(vs) if vs else 0
    quote_is_top = dom_means.get("quoteCount", 0) >= max(dom_means.values()) if dom_means else False
    quote_mean = dom_means.get("quoteCount", 0)
    top_dom = max(dom_means, key=dom_means.get) if dom_means else "N/A"
    top_dom_mean = dom_means.get(top_dom, 0)
    verdict = "CONFIRMED" if quote_is_top else "CONTRADICTED"
    field_kr = {"likeCount": "좋아요", "retweetCount": "리트윗", "replyCount": "답글",
                "bookmarkCount": "북마크", "quoteCount": "인용"}
    rules.append({
        "rule": "인용트윗 dominant = 최고 조회",
        "method": "dominant type별 평균 조회수",
        "measured": f"인용 dominant 평균={round(quote_mean):,}, 최고 dominant={field_kr.get(top_dom, top_dom)} ({round(top_dom_mean):,})",
        "verdict": verdict,
        "detail": f"Dominant별 평균: {', '.join(f'{field_kr.get(k,k)}={round(v):,}' for k, v in sorted(dom_means.items(), key=lambda x: x[1], reverse=True))}",
    })

    # 규칙 8: DM 공유가 알고리즘 최우선 (알고리즘 소스 확인 only)
    rules.append({
        "rule": "DM 공유(share_via_dm)가 알고리즘에서 별도 가중치",
        "method": "알고리즘 소스코드 확인",
        "measured": "weighted_scorer.rs line 57: share_via_dm_score × SHARE_VIA_DM_WEIGHT 확인",
        "verdict": "ALGORITHM-ONLY",
        "detail": "데이터로 검증 불가. 알고리즘 코드에 별도 시그널 존재 확인",
    })

    # 규칙 9: 작성자 다양성 페널티
    rules.append({
        "rule": "작성자 다양성 페널티 (연타 포스팅 감쇠)",
        "method": "알고리즘 소스코드 확인",
        "measured": "author_diversity_scorer.rs: multiplier = (1-floor) × decay^position + floor 확인",
        "verdict": "ALGORITHM-ONLY",
        "detail": "데이터로 검증 불가. 알고리즘 코드에 지수적 감쇠 존재 확인",
    })

    return rules


# ─────────────────────────────────────────────
# Step 7. 우선순위 랭킹
# ─────────────────────────────────────────────

def compute_priority_ranking(engagement_corr, text_corr, algo_signals,
                             hook_effects, body_effects):
    """최종 점수 = 0.4 × 상관강도 + 0.3 × 효과크기 + 0.3 × 알고리즘중요도."""
    items = []

    # 참여지표
    algo_importance = {
        "likeCount": 0.9,  # favorite: 가장 기본
        "retweetCount": 0.8,
        "replyCount": 0.7,
        "bookmarkCount": 0.6,  # share 프록시
        "quoteCount": 0.6,
    }
    for r in engagement_corr:
        corr_strength = abs(r["pearson_r"])
        # 효과 크기 대신 spearman으로 대체
        effect = abs(r["spearman_rho"])
        algo_imp = algo_importance.get(r["field"], 0.5)
        score = 0.4 * corr_strength + 0.3 * effect + 0.3 * algo_imp
        items.append({
            "factor": f"참여지표: {r['name_kr']}",
            "corr_strength": round(corr_strength, 4),
            "metric_type": "Pearson |r|",
            "effect_size": round(effect, 4),
            "algo_importance": algo_imp,
            "final_score": round(score, 4),
        })

    # 텍스트 피처
    text_algo = {
        "text_length": 0.7,  # dwell_time 프록시
        "line_count": 0.6,  # dwell_time 프록시
        "has_url": 0.5,  # photo_expand 프록시
        "hashtag_count": 0.1,  # 알고리즘 무관
        "has_emoji": 0.2,
        "has_question": 0.3,
        "has_number_list": 0.4,
        "has_arrow": 0.4,
        "word_count": 0.5,
    }
    for r in text_corr:
        corr_strength = abs(r["pearson_r"])
        effect = abs(r.get("cohens_d", 0))
        # 효과 크기 정규화 (Cohen's d는 0.8 이상이 large)
        effect_norm = min(effect / 0.8, 1.0)
        algo_imp = text_algo.get(r["field"], 0.3)
        score = 0.4 * corr_strength + 0.3 * effect_norm + 0.3 * algo_imp
        items.append({
            "factor": f"텍스트: {r['name_kr']}",
            "corr_strength": round(corr_strength, 4),
            "metric_type": "Pearson |r|",
            "effect_size": round(effect_norm, 4),
            "algo_importance": algo_imp,
            "final_score": round(score, 4),
        })

    # Hook 유형 (상위 효과 크기)
    # LLM 전용 유형: 고성과만 존재 → Cohen's d 편향 → 별도 스코어링
    # 비율/평균 기반 정규화를 위한 max 값 사전 계산
    llm_labels = {l for l in hook_effects if hook_effects[l].get("bias_note")}
    if llm_labels:
        llm_ratios = {}
        llm_means = {}
        for label in llm_labels:
            data = hook_effects[label]
            # 고/저 비율: 저성과 0건이므로 count 자체를 비율 프록시로 사용
            llm_ratios[label] = data["count"]
            llm_means[label] = data["mean_views"]
        max_ratio = max(llm_ratios.values()) if llm_ratios else 1
        max_mean = max(llm_means.values()) if llm_means else 1
    else:
        max_ratio = 1
        max_mean = 1

    for label, data in hook_effects.items():
        kr = HOOK_LABELS_KR.get(label, label)

        if label in llm_labels:
            # LLM 전용 유형: 별도 스코어링
            corr_str = llm_ratios[label] / max_ratio if max_ratio else 0
            effect_norm = llm_means[label] / max_mean if max_mean else 0
            algo_imp = 0.5  # 간접
            score = 0.4 * corr_str + 0.3 * effect_norm + 0.3 * algo_imp
            items.append({
                "factor": f"Hook: {kr} (LLM)",
                "corr_strength": round(corr_str, 4),
                "metric_type": "빈도비율",
                "effect_size": round(effect_norm, 4),
                "algo_importance": algo_imp,
                "final_score": round(score, 4),
                "n": data["count"],
            })
        else:
            # 기존 Cohen's d 기반 공식
            d = abs(data["cohens_d"])
            effect_norm = min(d / 0.8, 1.0)  # Cohen's d 0.8 = large (통일 기준)
            algo_imp = 0.5  # 간접 (사용자 반응 유도)
            corr_str = effect_norm * 0.5  # 프록시
            score = 0.4 * corr_str + 0.3 * effect_norm + 0.3 * algo_imp
            items.append({
                "factor": f"Hook: {kr}",
                "corr_strength": round(corr_str, 4),
                "metric_type": "효과크기 프록시",
                "effect_size": round(effect_norm, 4),
                "algo_importance": algo_imp,
                "final_score": round(score, 4),
                "n": data["count"],
            })

    # Body 유형
    for label, data in body_effects.items():
        d = abs(data["cohens_d"])
        effect_norm = min(d / 0.8, 1.0)  # Cohen's d 0.8 = large (통일 기준)
        algo_imp = 0.6  # dwell_time 관련
        corr_str = effect_norm * 0.5
        score = 0.4 * corr_str + 0.3 * effect_norm + 0.3 * algo_imp
        kr = BODY_LABELS_KR.get(label, label)
        items.append({
            "factor": f"Body: {kr}",
            "corr_strength": round(corr_str, 4),
            "metric_type": "효과크기 프록시",
            "effect_size": round(effect_norm, 4),
            "algo_importance": algo_imp,
            "final_score": round(score, 4),
            "n": data["count"],
        })

    items.sort(key=lambda x: x["final_score"], reverse=True)

    # 우선순위 분류
    for i, item in enumerate(items):
        if item["final_score"] >= 0.5:
            item["priority"] = "HIGH"
        elif item["final_score"] >= 0.3:
            item["priority"] = "MEDIUM"
        else:
            item["priority"] = "LOW"

    return items


# ─────────────────────────────────────────────
# Step 8. 리포트 생성
# ─────────────────────────────────────────────

def fmt(n):
    if isinstance(n, float):
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M"
        elif n >= 1_000:
            return f"{n/1_000:.1f}K"
        elif abs(n) < 1:
            return f"{n:.4f}"
        return f"{n:.1f}"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def p_str(p):
    if p < 0.001:
        return "<0.001"
    elif p < 0.01:
        return f"{p:.3f}"
    elif p < 0.05:
        return f"{p:.3f}"
    else:
        return f"{p:.3f}"


def generate_report(total, high_count, low_count,
                    high_orig_count, low_orig_count,
                    engagement_corr, text_corr,
                    cross_matrix, cross_names,
                    dist_analysis, hook_effects, body_effects,
                    combo_analysis, algo_mapping,
                    rule_verdicts, priority_ranking,
                    hook_unclassified=0, body_unclassified=0,
                    total_features=0):
    lines = []
    def w(s=""):
        lines.append(s)

    # ── 1. 요약 ──
    confirmed = sum(1 for r in rule_verdicts if "CONFIRMED" in r["verdict"])
    contradicted = sum(1 for r in rule_verdicts if r["verdict"] == "CONTRADICTED")
    algo_only = sum(1 for r in rule_verdicts if r["verdict"] == "ALGORITHM-ONLY")
    total_rules = len(rule_verdicts)

    w("# 트윗 분석 전면 점검 — 검증 보고서")
    w()
    w("---")
    w()
    w("## 1. 요약")
    w()
    w(f"- **검증 대상**: {total:,}건 트윗 (56개 계정)")
    w(f"- **고성과(≥100K)**: {high_count:,}건, **저성과**: {low_count:,}건")
    w(f"- **분석 대상 (오리지널)**: 고성과 {high_orig_count:,}건 + 저성과 {low_orig_count:,}건")
    w()
    w(f"### 규칙 판정 결과")
    w()
    w(f"| 판정 | 건수 |")
    w(f"|:---|:---:|")
    w(f"| CONFIRMED | {confirmed} |")
    w(f"| CONTRADICTED | {contradicted} |")
    w(f"| ALGORITHM-ONLY | {algo_only} |")
    w(f"| **합계** | **{total_rules}** |")
    w()

    high_items = [i for i in priority_ranking if i["priority"] == "HIGH"]
    med_items = [i for i in priority_ranking if i["priority"] == "MEDIUM"]
    low_items = [i for i in priority_ranking if i["priority"] == "LOW"]
    w(f"### 우선순위 분포")
    w()
    w(f"- **HIGH**: {len(high_items)}개 요소")
    w(f"- **MEDIUM**: {len(med_items)}개 요소")
    w(f"- **LOW**: {len(low_items)}개 요소")
    w()

    # 수정 E — 방법론적 한계 명시
    w("### 방법론적 한계")
    w()
    w("> ⚠️ 본 보고서의 규칙은 동일 데이터셋에서 도출 및 검증되었습니다.")
    w("> CONFIRMED 판정은 이 데이터에서의 유효성이며, 새로운 데이터 일반화를 보장하지 않습니다.")
    w()

    # ── 2. 상관관계 분석 ──
    w("## 2. 상관관계 분석")
    w()
    w("### 2.1 참여지표 vs viewCount")
    w()
    w("| 참여 유형 | Pearson r | p-value | Spearman ρ | p-value | 해석 |")
    w("|:---|:---:|:---:|:---:|:---:|:---|")
    for r in sorted(engagement_corr, key=lambda x: abs(x["pearson_r"]), reverse=True):
        pr = r["pearson_r"]
        if abs(pr) >= 0.7:
            interp = "강한 상관"
        elif abs(pr) >= 0.4:
            interp = "중간 상관"
        elif abs(pr) >= 0.2:
            interp = "약한 상관"
        else:
            interp = "매우 약한"
        w(f"| **{r['name_kr']}** | {pr} | {p_str(r['pearson_p'])} | {r['spearman_rho']} | {p_str(r['spearman_p'])} | {interp} |")
    w()

    w("### 2.2 텍스트 피처 vs viewCount")
    w()
    w("| 피처 | Pearson r | Spearman ρ | Cohen's d (고/저) | 해석 |")
    w("|:---|:---:|:---:|:---:|:---|")
    for r in sorted(text_corr, key=lambda x: abs(x["pearson_r"]), reverse=True):
        d = r["cohens_d"]
        if abs(d) >= 0.8:
            d_interp = "큰 효과"
        elif abs(d) >= 0.5:
            d_interp = "중간 효과"
        elif abs(d) >= 0.2:
            d_interp = "작은 효과"
        else:
            d_interp = "무시 가능"
        w(f"| {r['name_kr']} | {r['pearson_r']} | {r['spearman_rho']} | {r['cohens_d']} ({d_interp}) | {'유의' if abs(r['pearson_r']) >= 0.05 else '무효과'} |")
    w()

    # ── 3. 교차 상관 매트릭스 ──
    w("## 3. 교차 상관 매트릭스 (참여지표 간)")
    w()
    header = "| | " + " | ".join(cross_names) + " |"
    w(header)
    w("|:---|" + ":---:|" * len(cross_names))
    for ni in cross_names:
        row = f"| **{ni}** |"
        for nj in cross_names:
            key = (ni, nj) if (ni, nj) in cross_matrix else (nj, ni)
            r, p = cross_matrix.get(key, (0, 1))
            row += f" {r} |"
        w(row)
    w()

    # ── 4. 분포 분석 ──
    w("## 4. 분포 분석 (고성과 오리지널)")
    w()
    for metric_name, data in dist_analysis.items():
        w(f"### {metric_name}")
        w()
        w(f"- **평균**: {fmt(data['mean'])}")
        w(f"- **분포**: {data['skew']}")
        w()
        pcts = data["percentiles"]
        w("| 백분위 | 10 | 25 | 50 | 75 | 90 | 95 | 99 |")
        w("|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")
        vals = " | ".join(fmt(pcts[q]) for q in [10, 25, 50, 75, 90, 95, 99])
        w(f"| 값 | {vals} |")
        w()

    # ── 5. Hook/Body 유형 검증 ──
    w("## 5. Hook/Body 유형별 효과 검증")
    w()

    # 수정 D — 분류 커버리지 보고
    if total_features > 0:
        hook_uncl_pct = hook_unclassified / total_features * 100
        body_uncl_pct = body_unclassified / total_features * 100
        w("### 분류 커버리지")
        w()
        w(f"- Hook: unclassified {hook_unclassified}건 / 전체 {total_features}건 ({hook_uncl_pct:.1f}%)")
        w(f"- Body: unclassified {body_unclassified}건 / 전체 {total_features}건 ({body_uncl_pct:.1f}%)")
        w()

    # 수정 F — 다중 레이블 특성 명시
    w("> 참고: Hook과 Body는 다중 레이블 분류입니다. 하나의 트윗이 여러 유형에 동시 분류될 수 있어 N 합계가 전체 트윗 수를 초과합니다.")
    w()

    w("### 5.1 Hook 유형별")
    w()
    w("| Hook 유형 | N | 평균 조회수 | 95% CI | Cohen's d | 판정 | 비고 |")
    w("|:---|:---:|:---:|:---:|:---:|:---|:---|")
    for label in sorted(hook_effects, key=lambda x: hook_effects[x]["mean_views"], reverse=True):
        data = hook_effects[label]
        kr = HOOK_LABELS_KR.get(label, label)
        d = data["cohens_d"]
        if abs(d) >= 0.5:
            judge = "유의미"
        elif abs(d) >= 0.2:
            judge = "약한 효과"
        else:
            judge = "효과 없음"
        bias = data.get("bias_note", "")
        if data["count"] < 10:
            judge += " ⚠️소표본"
        w(f"| {kr} | {data['count']:,} | {fmt(data['mean_views'])} | [{fmt(data['ci_low'])}, {fmt(data['ci_high'])}] | {d} | {judge} | {bias} |")
    w()

    w("### 5.2 Body 유형별")
    w()
    w("| Body 유형 | N | 평균 조회수 | 95% CI | Cohen's d | 판정 |")
    w("|:---|:---:|:---:|:---:|:---:|:---|")
    for label in sorted(body_effects, key=lambda x: body_effects[x]["mean_views"], reverse=True):
        data = body_effects[label]
        kr = BODY_LABELS_KR.get(label, label)
        d = data["cohens_d"]
        if abs(d) >= 0.5:
            judge = "유의미"
        elif abs(d) >= 0.2:
            judge = "약한 효과"
        else:
            judge = "효과 없음"
        if data["count"] < 10:
            judge += " ⚠️소표본"
        w(f"| {kr} | {data['count']:,} | {fmt(data['mean_views'])} | [{fmt(data['ci_low'])}, {fmt(data['ci_high'])}] | {d} | {judge} |")
    w()

    # 텍스트 피처 조합
    if combo_analysis:
        w("### 5.3 텍스트 피처 조합 분석")
        w()
        w("| 조합 | N | 조합 평균 조회 | 비조합 평균 조회 | Cohen's d |")
        w("|:---|:---:|:---:|:---:|:---:|")
        for c in combo_analysis:
            w(f"| {c['combo']} | {c['count']:,} | {fmt(c['mean_views_match'])} | {fmt(c['mean_views_non'])} | {c['cohens_d']} |")
        w()

    # ── 6. X 알고리즘 시그널 매핑 ──
    w("## 6. X 알고리즘 시그널 매핑")
    w()
    w("| 알고리즘 시그널 | 설명 | 데이터 매핑 | 측정 가능성 | Pearson r | Spearman ρ |")
    w("|:---|:---|:---|:---:|:---:|:---:|")
    for s in algo_mapping:
        pr = f"{s['pearson_r']}" if s["pearson_r"] is not None else "—"
        sr = f"{s['spearman_rho']}" if s["spearman_rho"] is not None else "—"
        data_field = s.get("data_field", "—")
        w(f"| {s['signal']} | {s['description']} | {data_field} | {s['measurability']} | {pr} | {sr} |")
    w()

    # ── 7. 규칙별 판정 결과 ──
    w("## 7. 규칙별 판정 결과")
    w()
    w("| # | 규칙 | 검증 방법 | 측정값 | 판정 |")
    w("|:---:|:---|:---|:---|:---:|")
    for i, r in enumerate(rule_verdicts, 1):
        emoji = {"CONFIRMED": "✅", "CONTRADICTED": "❌", "ALGORITHM-ONLY": "🔧"}.get(
            r["verdict"].split(" ")[0], "⚠️")
        if "CONFIRMED" in r["verdict"]:
            emoji = "✅"
        w(f"| {i} | {r['rule']} | {r['method']} | {r['measured']} | {emoji} {r['verdict']} |")
    w()
    w("### 상세 설명")
    w()
    for i, r in enumerate(rule_verdicts, 1):
        w(f"**{i}. {r['rule']}**: {r['detail']}")
        w()

    # ── 8. 우선순위 랭킹 ──
    w("## 8. 우선순위 랭킹")
    w()
    w("> 최종 점수 = 0.4 × 상관강도 + 0.3 × 효과크기 + 0.3 × 알고리즘중요도")
    w()

    w("> ⚠️ 관련성지표는 카테고리별 산출 방식이 다르므로 직접 비교 불가")
    w()

    for priority_label in ["HIGH", "MEDIUM", "LOW"]:
        group = [i for i in priority_ranking if i["priority"] == priority_label]
        if not group:
            continue
        w(f"### {priority_label} 우선순위")
        w()
        w("| 요소 | 관련성지표 | 지표유형 | 효과크기 | 알고리즘 | 최종점수 |")
        w("|:---|:---:|:---:|:---:|:---:|:---:|")
        for item in group:
            mt = item.get("metric_type", "—")
            n_val = item.get("n")
            factor_display = item["factor"]
            if n_val is not None and n_val < 10:
                factor_display += f" (N={n_val})"
            w(f"| {factor_display} | {item['corr_strength']} | {mt} | {item['effect_size']} | {item['algo_importance']} | **{item['final_score']}** |")
        w()

    # ── 9. 권고사항 ──
    w("## 9. 권고사항")
    w()

    # 자동 생성 권고
    w("### 데이터 기반 권고")
    w()

    # 가장 강한 참여 지표
    strongest_eng = max(engagement_corr, key=lambda x: abs(x["pearson_r"]))
    w(f"1. **{strongest_eng['name_kr']}이 조회수와 가장 강한 상관** (r={strongest_eng['pearson_r']}, ρ={strongest_eng['spearman_rho']})")
    w(f"   → {strongest_eng['name_kr']}를 유도하는 콘텐츠가 조회수 확대에 가장 효과적")
    w()

    # 텍스트 길이
    best_text = max(text_corr, key=lambda x: abs(x["cohens_d"]))
    w(f"2. **{best_text['name_kr']}이 고/저성과 간 가장 큰 차이** (Cohen's d={best_text['cohens_d']})")
    w(f"   → {best_text['name_kr']} 최적화가 가장 실질적인 성과 차이를 만듦")
    w()

    # HIGH 우선순위 요약
    w("3. **HIGH 우선순위 요소 집중**:")
    for item in priority_ranking[:5]:
        if item["priority"] == "HIGH":
            w(f"   - {item['factor']} (점수: {item['final_score']})")
    w()

    # 확인된 규칙
    confirmed_rules = [r for r in rule_verdicts if "CONFIRMED" in r["verdict"]]
    if confirmed_rules:
        w("4. **검증된 규칙 유지**:")
        for r in confirmed_rules:
            w(f"   - ✅ {r['rule']}")
    w()

    # 반박된 규칙
    contradicted_rules = [r for r in rule_verdicts if r["verdict"] == "CONTRADICTED"]
    if contradicted_rules:
        w("5. **반박된 규칙 재검토**:")
        for r in contradicted_rules:
            w(f"   - ❌ {r['rule']}: {r['detail']}")
    w()

    w("### 알고리즘 기반 권고 (측정 불가 영역)")
    w()
    w("- **DM 공유 유도**: share_via_dm이 별도 가중치. '친구한테 보내고 싶은' 콘텐츠 설계")
    w("- **이미지 확대 유도**: photo_expand 별도 가중치. 클릭해서 자세히 보고 싶은 이미지")
    w("- **프로필 클릭 유도**: profile_click 별도 가중치. '이 사람 누구지?' 유발")
    w("- **연타 금지**: author_diversity_scorer에 의한 지수적 감쇠. 시간 간격 필요")
    w("- **부정 반응 최소화**: block/mute/report 음의 가중치. 공격적 톤 회피")
    w()

    # LLM 유형 편향 주의사항
    llm_hook_labels = [l for l, d in hook_effects.items() if d.get("bias_note")]
    if llm_hook_labels:
        w("### ⚠️ LLM 분류 유형 주의사항")
        w()
        w("아래 Hook 유형은 **LLM 캐시(고성과 137건)에서만 분류**되어 저성과 트윗과의 비교가 불가합니다.")
        w("Cohen's d가 인위적으로 높게 나오므로, 우선순위 랭킹에서 별도 스코어링(빈도/평균 기반)을 적용했습니다.")
        w()
        for label in llm_hook_labels:
            kr = HOOK_LABELS_KR.get(label, label)
            data = hook_effects[label]
            w(f"- **{kr}**: N={data['count']}, 평균={fmt(data['mean_views'])}")
        w()

    return "\n".join(lines)


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    print("=" * 60)
    print("트윗 분석 전면 점검 — 상관관계 검증 & 우선순위 분류")
    print("=" * 60)

    # Step 1: 데이터 로드
    print("\n[Step 1] 데이터 로드...")
    tweets, skip_view, skip_dup = load_all_tweets()
    print(f"  로드 완료: {len(tweets):,}건 (viewCount 누락 {skip_view:,}, 중복 {skip_dup:,} 제외)")

    high_orig, high_rt, low_orig, low_rt = split_tweets(tweets)
    high_count = len(high_orig) + len(high_rt)
    low_count = len(low_orig) + len(low_rt)
    print(f"  고성과: {high_count:,} (오리지널 {len(high_orig):,}, RT {len(high_rt):,})")
    print(f"  저성과: {low_count:,} (오리지널 {len(low_orig):,}, RT {len(low_rt):,})")

    # Step 2: 피처 추출 (오리지널만)
    print("\n[Step 2] 피처 추출 (오리지널 트윗)...")
    all_orig = high_orig + low_orig
    features = [extract_features(t) for t in all_orig]
    high_features = [f for f in features if f["viewCount"] >= THRESHOLD]
    low_features = [f for f in features if f["viewCount"] < THRESHOLD]
    print(f"  피처 추출 완료: {len(features):,}건")

    # Step 4a: 참여지표 vs viewCount
    print("\n[Step 4a] 참여지표 vs viewCount 상관관계...")
    engagement_corr = analysis_4a(features)
    for r in engagement_corr:
        print(f"  {r['name_kr']}: Pearson r={r['pearson_r']}, Spearman ρ={r['spearman_rho']}")

    # Step 4b: 텍스트 피처 vs viewCount
    print("\n[Step 4b] 텍스트 피처 vs viewCount...")
    text_corr = analysis_4b(features, high_features, low_features)
    for r in text_corr:
        print(f"  {r['name_kr']}: r={r['pearson_r']}, Cohen's d={r['cohens_d']}")

    # Step 4c: 교차 상관 매트릭스
    print("\n[Step 4c] 참여지표 간 교차 상관 매트릭스...")
    cross_matrix, cross_names = analysis_4c(features)
    print(f"  {len(cross_matrix)}개 쌍 계산 완료")

    # Step 4d: 분포 분석
    print("\n[Step 4d] 분포 분석 (고성과 오리지널)...")
    dist_analysis = analysis_4d(high_features)
    for metric, data in dist_analysis.items():
        print(f"  {metric}: 평균={fmt(data['mean'])}, 중위수={fmt(data['percentiles'][50])}, {data['skew']}")

    # Step 4e: Hook/Body 유형별 효과
    print("\n[Step 4e] Hook/Body 유형별 효과 검증...")
    hook_effects, body_effects, hook_uncl, body_uncl = analysis_4e(features, high_features, low_features)
    print(f"  Hook 유형: {len(hook_effects)}개, Body 유형: {len(body_effects)}개")
    print(f"  unclassified: Hook {hook_uncl}건, Body {body_uncl}건")

    # Step 4f: 텍스트 피처 조합
    print("\n[Step 4f] 텍스트 피처 조합 분석...")
    combo_analysis = analysis_4f(features)
    for c in combo_analysis:
        print(f"  {c['combo']}: N={c['count']}, d={c['cohens_d']}")

    # Step 5: 알고리즘 시그널 매핑
    print("\n[Step 5] X 알고리즘 시그널 매핑...")
    algo_mapping = analysis_5(features)
    measurable = sum(1 for s in algo_mapping if s["pearson_r"] is not None)
    print(f"  측정 가능: {measurable}/19개 시그널")

    # Step 6: 규칙 검증
    print("\n[Step 6] 규칙 검증...")
    rule_verdicts = validate_rules(features, high_features, low_features,
                                   engagement_corr, text_corr,
                                   hook_effects, body_effects)
    for r in rule_verdicts:
        icon = {"CONFIRMED": "✅", "CONTRADICTED": "❌", "ALGORITHM-ONLY": "🔧"}.get(
            r["verdict"].split(" ")[0], "⚠️")
        if "CONFIRMED" in r["verdict"]:
            icon = "✅"
        print(f"  {icon} {r['rule']}: {r['verdict']}")

    # Step 7: 우선순위 랭킹
    print("\n[Step 7] 우선순위 랭킹...")
    priority_ranking = compute_priority_ranking(engagement_corr, text_corr,
                                                algo_mapping,
                                                hook_effects, body_effects)
    for p_label in ["HIGH", "MEDIUM", "LOW"]:
        group = [i for i in priority_ranking if i["priority"] == p_label]
        print(f"  {p_label}: {len(group)}개")
        for item in group[:3]:
            print(f"    - {item['factor']} ({item['final_score']})")

    # Step 8: 리포트 생성
    print("\n[Step 8] 리포트 생성...")
    report = generate_report(
        total=len(tweets),
        high_count=high_count,
        low_count=low_count,
        high_orig_count=len(high_orig),
        low_orig_count=len(low_orig),
        engagement_corr=engagement_corr,
        text_corr=text_corr,
        cross_matrix=cross_matrix,
        cross_names=cross_names,
        dist_analysis=dist_analysis,
        hook_effects=hook_effects,
        body_effects=body_effects,
        combo_analysis=combo_analysis,
        algo_mapping=algo_mapping,
        rule_verdicts=rule_verdicts,
        priority_ranking=priority_ranking,
        hook_unclassified=hook_uncl,
        body_unclassified=body_uncl,
        total_features=len(features),
    )

    with open(OUTPUT_MD, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  저장 완료: {OUTPUT_MD}")

    print("\n" + "=" * 60)
    confirmed = sum(1 for r in rule_verdicts if "CONFIRMED" in r["verdict"])
    contradicted = sum(1 for r in rule_verdicts if r["verdict"] == "CONTRADICTED")
    print(f"완료! {len(rule_verdicts)}개 규칙 중 {confirmed}개 확인, {contradicted}개 반박")
    print(f"결과: {OUTPUT_MD}")
    print("=" * 60)


if __name__ == "__main__":
    main()
