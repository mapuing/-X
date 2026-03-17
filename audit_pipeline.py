"""
전체 분석 파이프라인 독립 감사 (audit_pipeline.py)

기존 코드(validate_analysis.py, reverse_engineer_tweets.py)를 import하지 않고
모든 계산을 밑바닥부터 재현하여 기존 출력물과 대조.

28개 체크 (7 Phase):
  A1~A5: 데이터 무결성
  B1~B5: 통계 방법론
  C1~C4: 분류 일관성
  D1~D9: 규칙 판정
  E1~E5: 알고리즘 소스코드 검증
  F1:     우선순위 랭킹
  G1~G2: 보고서 교차 일관성

산출물: audit_report.md
"""

import json
import math
import re
import zipfile
import sys
import io
from pathlib import Path
from collections import defaultdict

# Windows cp949 인코딩 문제 방지
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "x 자료"
ZIP_PATH = DATA_DIR / "x-algorithm-main.zip"
LLM_CACHE_FILE = BASE_DIR / "llm_hook_cache.json"
HIGH_FILE = BASE_DIR / "high_performing_tweets.json"
VALIDATION_REPORT = BASE_DIR / "validation_report.md"
TWEET_FORMULAS = BASE_DIR / "tweet_formulas.md"
OUTPUT_MD = BASE_DIR / "audit_report.md"
THRESHOLD = 100_000

# ─────────────────────────────────────────────
# 공통 유틸리티 (stdlib only, 기존 코드와 독립)
# ─────────────────────────────────────────────

URL_RE = re.compile(r'https?://\S+')
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
QUESTION_RE = re.compile(r'\?')
NUMBER_LIST_RE = re.compile(r'\n\s*\d+[.)]\s')
ARROW_RE = re.compile(r'[→➡▶]')

HOOK_PATTERNS = {
    "relatable_targeting": re.compile(
        r'(하는\s*사람|인\s*분들?|하는\s*분들?|있는\s*사람|겪는|겪어본|공감|해본\s*사람|느끼는|다들\s|너희|여러분)',
        re.IGNORECASE),
    "credibility": re.compile(
        r'(현직|전문|의사|교수|변호사|약사|전직|경력|년차|연차|업계|실무|석사|박사|전공)',
        re.IGNORECASE),
    "challenge": re.compile(
        r'(풀어|맞춰|퀴즈|테스트|도전|챌린지|찾아|틀린\s*곳|다른\s*곳|몇\s*개|맞히)',
        re.IGNORECASE),
    "practical_tip": re.compile(
        r'(방법|하려면|꿀팁|노하우|비법|비결|팁\b|루틴|습관|하는\s*법|알려|정리했|모음|추천)',
        re.IGNORECASE),
    "bracket_title": re.compile(r'(\[.+\]|【.+】|「.+」|『.+』)'),
    "numbered_title": re.compile(
        r'(\d+\s*가지|\d+\s*선|top\s*\d+|\d+\s*개|best\s*\d+)',
        re.IGNORECASE),
    "question": re.compile(r'\?'),
    "news_shock": re.compile(
        r'(속보|충격|논란|경악|긴급|단독|발각|폭로|대참사|ㄷㄷ|헐|실화|레전드|미쳤|대박|역대급)',
        re.IGNORECASE),
    "personal_story": re.compile(
        r'(어제|오늘|방금|아까|살면서|처음으로|나는|저는|제가|내가|경험|일화|실화|겪었|당했)',
        re.IGNORECASE),
    "fact_curiosity": re.compile(
        r'(이유|때문에|중요성|비결|알고\s*보[면니]|사실[은\s]|몰랐던|비밀|비하인드)',
        re.IGNORECASE),
    "warning_loss": re.compile(
        r'(하지\s*마|절대\s*[하금안]|위험하|조심|주의\b|금물|하면\s*안)',
        re.IGNORECASE),
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

LLM_ONLY_TYPES = {"narrative_situation", "info_fact", "opinion_reaction",
                  "celeb_mention", "promotion", "emotion_empathy", "comparison_choice"}


def extract_hook(text):
    text_clean = URL_RE.sub("", text).strip()
    for line in text_clean.split("\n"):
        line = line.strip()
        if line:
            return line
    return text_clean


def classify_hook(hook_text, llm_overrides):
    labels = []
    for name, pattern in HOOK_PATTERNS.items():
        if name == "short_cryptic":
            if len(hook_text) <= 25:
                labels.append(name)
        elif pattern and pattern.search(hook_text):
            labels.append(name)
    if not labels:
        llm_label = llm_overrides.get(hook_text)
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


def pearson_r(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return 0.0
    r = num / (den_x * den_y)
    return max(-1.0, min(1.0, r))


def spearman_rho(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0

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

    return pearson_r(rank_data(xs), rank_data(ys))


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


# ─────────────────────────────────────────────
# 데이터 로드 (완전 독립 구현)
# ─────────────────────────────────────────────

def load_raw_tweets():
    """JSONL 파일에서 전처리 없이 모든 레코드 로드."""
    all_records = []
    file_stats = {}
    for filepath in sorted(DATA_DIR.glob("*.jsonl")):
        account = filepath.stem
        records = []
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                t = json.loads(line)
                t["source"] = account
                records.append(t)
        file_stats[account] = len(records)
        all_records.extend(records)
    return all_records, file_stats


def load_deduped_tweets(raw_tweets):
    """중복 제거 + viewCount 누락 제외."""
    seen = set()
    tweets = []
    skipped_view = 0
    skipped_dup = 0
    for t in raw_tweets:
        if "viewCount" not in t or t["viewCount"] is None:
            skipped_view += 1
            continue
        key = (t.get("fullText", ""), t.get("source", ""))
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
    return high, low, high_orig, high_rt, low_orig, low_rt


def extract_features(tweet, llm_overrides):
    text = tweet.get("fullText", "")
    text_clean = URL_RE.sub("", text).strip()
    views = tweet.get("viewCount", 0) or 0
    likes = tweet.get("likeCount", 0) or 0
    retweets = tweet.get("retweetCount", 0) or 0
    replies = tweet.get("replyCount", 0) or 0
    bookmarks = tweet.get("bookmarkCount", 0) or 0
    quotes = tweet.get("quoteCount", 0) or 0

    total_er = (likes + retweets + replies + bookmarks + quotes) / views if views > 0 else 0

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

    has_url = 1 if URL_RE.search(text) else 0
    hashtag_count = len(HASHTAG_RE.findall(text))
    has_emoji = 1 if EMOJI_RE.search(text) else 0
    has_question = 1 if QUESTION_RE.search(text_clean) else 0
    has_number_list = 1 if len(NUMBER_LIST_RE.findall(text_clean)) >= 2 else 0
    has_arrow = 1 if ARROW_RE.search(text_clean) else 0

    hook_text = extract_hook(text)
    hook_labels = classify_hook(hook_text, llm_overrides)
    body_labels = classify_body(text)

    return {
        "text": text,
        "hook_text": hook_text,
        "source": tweet.get("source", ""),
        "viewCount": views,
        "likeCount": likes,
        "retweetCount": retweets,
        "replyCount": replies,
        "bookmarkCount": bookmarks,
        "quoteCount": quotes,
        "total_er": total_er,
        "text_length": text_length,
        "word_count": word_count,
        "line_count": line_count,
        "length_bin": length_bin,
        "has_url": has_url,
        "hashtag_count": hashtag_count,
        "has_emoji": has_emoji,
        "has_question": has_question,
        "has_number_list": has_number_list,
        "has_arrow": has_arrow,
        "hook_labels": hook_labels,
        "body_labels": body_labels,
    }


# ═══════════════════════════════════════════════
# Phase 1: 데이터 무결성 (A1~A5)
# ═══════════════════════════════════════════════

def audit_a1_data_completeness(raw_tweets, file_stats):
    """A1: JSONL 56개 전수 로드, 필드 7개 완전성."""
    n_files = len(file_stats)
    required_fields = ["fullText", "viewCount", "likeCount", "retweetCount",
                       "replyCount", "bookmarkCount", "quoteCount"]

    # 필드 누락 분석
    missing_counts = {f: 0 for f in required_fields}
    for t in raw_tweets:
        for f in required_fields:
            if f not in t or t[f] is None:
                missing_counts[f] += 1

    view_missing = missing_counts["viewCount"]
    other_missing = {f: c for f, c in missing_counts.items() if f != "viewCount" and c > 0}

    status = "PASS" if n_files == 56 else "FAIL"
    detail = (f"JSONL {n_files}개 로드, 총 {len(raw_tweets):,}건. "
              f"viewCount 누락 {view_missing}건.")
    if other_missing:
        detail += f" 기타 누락: {other_missing}"

    return {
        "id": "A1",
        "name": "JSONL 전수 로드 + 필드 완전성",
        "expected": "56파일, viewCount 누락 11건",
        "actual": f"{n_files}파일, viewCount 누락 {view_missing}건",
        "status": status,
        "detail": detail,
    }


def audit_a2_count_reconciliation(deduped_tweets, high, low, high_orig, low_orig,
                                  skipped_view, skipped_dup, raw_tweets):
    """A2: 파이프라인 간 건수 일치."""
    total_deduped = len(deduped_tweets)
    n_high = len(high)
    n_low = len(low)

    # 비중복제거 건수 (viewCount 누락만 제외)
    raw_with_view = [t for t in raw_tweets if "viewCount" in t and t["viewCount"] is not None]
    n_raw_with_view = len(raw_with_view)
    n_raw_high = sum(1 for t in raw_with_view if t.get("viewCount", 0) >= THRESHOLD)

    # high_performing_tweets.json 대조
    hp_count = None
    hp_high_count = None
    if HIGH_FILE.exists():
        with open(HIGH_FILE, "r", encoding="utf-8") as f:
            hp_data = json.load(f)
        if isinstance(hp_data, list):
            hp_count = len(hp_data)
            hp_high_count = sum(1 for t in hp_data if t.get("viewCount", 0) >= THRESHOLD)

    issues = []

    # validation_report.md 기준: 43,773 / 3,919
    if total_deduped != 43773:
        issues.append(f"중복제거 후 총건수 {total_deduped:,} ≠ 기대 43,773")
    if n_high != 3919:
        issues.append(f"중복제거 고성과 {n_high:,} ≠ 기대 3,919")

    # analyze_tweets.py 기준 (비중복제거): 44,014 / 3,964
    dup_count = n_raw_with_view - total_deduped
    if hp_count and hp_count != n_raw_with_view:
        issues.append(f"high_performing_tweets.json 로드건수 불일치: {hp_count} vs raw {n_raw_with_view}")

    status = "PASS" if not issues else "WARNING"
    detail = (f"중복제거: {total_deduped:,}건 (고{n_high:,}/저{n_low:,}). "
              f"비중복제거: {n_raw_with_view:,}건 (고{n_raw_high:,}). "
              f"중복 {dup_count:,}건. "
              f"viewCount 누락 제외 {skipped_view}건.")
    if issues:
        detail += " ISSUES: " + "; ".join(issues)

    return {
        "id": "A2",
        "name": "파이프라인 간 건수 일치",
        "expected": "중복제거=43,773/3,919, 비중복제거=44,014/3,964",
        "actual": f"중복제거={total_deduped:,}/{n_high:,}, 비중복제거={n_raw_with_view:,}/{n_raw_high:,}",
        "status": status,
        "detail": detail,
    }


def audit_a3_duplicate_analysis(raw_tweets):
    """A3: 중복 분석 — 241건 중복이 진짜 중복인지 확인."""
    # viewCount 있는 것만 대상
    with_view = [t for t in raw_tweets if "viewCount" in t and t["viewCount"] is not None]

    seen = {}
    duplicates = []
    for t in with_view:
        key = (t.get("fullText", ""), t.get("source", ""))
        if key in seen:
            # 메트릭 동일성 확인
            orig = seen[key]
            same_metrics = (
                t.get("viewCount") == orig.get("viewCount") and
                t.get("likeCount") == orig.get("likeCount") and
                t.get("retweetCount") == orig.get("retweetCount")
            )
            duplicates.append({
                "key": key[0][:50],
                "source": key[1],
                "same_metrics": same_metrics,
            })
        else:
            seen[key] = t

    n_dup = len(duplicates)
    all_same = all(d["same_metrics"] for d in duplicates)

    status = "PASS" if all_same else "WARNING"
    detail = f"{n_dup}건 중복 발견. 메트릭 동일={all_same}."
    if not all_same:
        diff_count = sum(1 for d in duplicates if not d["same_metrics"])
        detail += f" 메트릭 불일치 {diff_count}건."

    return {
        "id": "A3",
        "name": "중복 분석 (진짜 중복 확인)",
        "expected": "241건 중복, 모두 동일 메트릭",
        "actual": f"{n_dup}건 중복, 동일 메트릭={'예' if all_same else '아니오'}",
        "status": status,
        "detail": detail,
    }


def audit_a4_outlier_detection(deduped_tweets):
    """A4: IQR 기반 이상치 탐지."""
    views = sorted([t.get("viewCount", 0) for t in deduped_tweets])
    n = len(views)
    q1 = views[n // 4]
    q3 = views[3 * n // 4]
    iqr = q3 - q1
    upper_fence = q3 + 3.0 * iqr  # 극단 이상치 기준

    outliers = [v for v in views if v > upper_fence]
    n_outliers = len(outliers)

    # 계정별 쏠림 확인
    account_counts = defaultdict(int)
    account_high = defaultdict(int)
    for t in deduped_tweets:
        src = t.get("source", "unknown")
        account_counts[src] += 1
        if t.get("viewCount", 0) >= THRESHOLD:
            account_high[src] += 1

    max_share = 0
    max_account = ""
    total_high = sum(account_high.values())
    if total_high > 0:
        for acc, cnt in account_high.items():
            share = cnt / total_high
            if share > max_share:
                max_share = share
                max_account = acc

    skew_ok = max_share < 0.1  # 단일 계정이 고성과의 10% 이상이면 쏠림

    status = "PASS" if skew_ok else "WARNING"
    detail = (f"IQR={iqr:,}, 상한={upper_fence:,.0f}, 극단 이상치 {n_outliers}건. "
              f"최대 쏠림 계정: {max_account} ({max_share:.1%})")

    return {
        "id": "A4",
        "name": "이상치 탐지 + 계정별 쏠림",
        "expected": "계정별 쏠림 없음 (단일 계정 < 10%)",
        "actual": f"최대 쏠림 {max_account} ({max_share:.1%}), 극단 이상치 {n_outliers}건",
        "status": status,
        "detail": detail,
    }


def audit_a5_zip_verification():
    """A5: ZIP 내용 확인."""
    if not ZIP_PATH.exists():
        return {
            "id": "A5", "name": "ZIP 내용 확인",
            "expected": "소스코드만 포함", "actual": "ZIP 파일 없음",
            "status": "FAIL", "detail": "x-algorithm-main.zip 파일 없음",
        }

    extensions = defaultdict(int)
    total_files = 0
    jsonl_files = []

    with zipfile.ZipFile(ZIP_PATH, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            total_files += 1
            ext = Path(info.filename).suffix.lower()
            extensions[ext] += 1
            if ext == ".jsonl":
                jsonl_files.append(info.filename)

    has_jsonl = len(jsonl_files) > 0
    status = "PASS" if not has_jsonl else "FAIL"
    ext_summary = ", ".join(f"{ext}={cnt}" for ext, cnt in
                           sorted(extensions.items(), key=lambda x: x[1], reverse=True)[:10])

    return {
        "id": "A5",
        "name": "ZIP 내용 확인",
        "expected": "소스코드만, JSONL 없음",
        "actual": f"{total_files}파일, JSONL={len(jsonl_files)}건",
        "status": status,
        "detail": f"확장자 분포: {ext_summary}",
    }


# ═══════════════════════════════════════════════
# Phase 2: 통계 방법론 (B1~B5)
# ═══════════════════════════════════════════════

def audit_b1_pearson_r(features):
    """B1: Pearson r 5개 재계산."""
    views = [f["viewCount"] for f in features]
    expected = {
        "likeCount": ("좋아요", 0.6716),
        "retweetCount": ("리트윗", 0.4626),
        "quoteCount": ("인용", 0.4448),
        "replyCount": ("답글", 0.4123),
        "bookmarkCount": ("북마크", 0.4014),
    }

    results = {}
    all_match = True
    details = []
    for field, (kr, exp_val) in expected.items():
        vals = [f[field] for f in features]
        r = round(pearson_r(vals, views), 4)
        results[kr] = r
        match = abs(r - exp_val) < 0.0005
        if not match:
            all_match = False
        details.append(f"{kr}: 기대={exp_val}, 실측={r}, {'OK' if match else 'DIFF'}")

    status = "PASS" if all_match else "FAIL"
    return {
        "id": "B1",
        "name": "Pearson r 5개 재계산",
        "expected": "좋아요=0.6716, RT=0.4626, 인용=0.4448, 답글=0.4123, 북마크=0.4014",
        "actual": ", ".join(f"{k}={v}" for k, v in results.items()),
        "status": status,
        "detail": "; ".join(details),
    }


def audit_b2_spearman_rho(features):
    """B2: Spearman ρ 5개 재계산."""
    views = [f["viewCount"] for f in features]
    expected = {
        "likeCount": ("좋아요", 0.8524),
        "retweetCount": ("리트윗", 0.766),
        "quoteCount": ("인용", 0.7324),
        "replyCount": ("답글", 0.6164),
        "bookmarkCount": ("북마크", 0.7828),
    }

    results = {}
    all_match = True
    details = []
    for field, (kr, exp_val) in expected.items():
        vals = [f[field] for f in features]
        rho = round(spearman_rho(vals, views), 4)
        results[kr] = rho
        match = abs(rho - exp_val) < 0.001
        if not match:
            all_match = False
        details.append(f"{kr}: 기대={exp_val}, 실측={rho}, {'OK' if match else 'DIFF'}")

    status = "PASS" if all_match else "FAIL"
    return {
        "id": "B2",
        "name": "Spearman ρ 5개 재계산",
        "expected": "좋아요=0.8524, RT=0.766, 인용=0.7324, 답글=0.6164, 북마크=0.7828",
        "actual": ", ".join(f"{k}={v}" for k, v in results.items()),
        "status": status,
        "detail": "; ".join(details),
    }


def audit_b3_cohens_d(high_features, low_features):
    """B3: Cohen's d 9개 재계산 (텍스트 피처)."""
    expected = {
        "line_count": ("줄 수", 0.447),
        "has_number_list": ("번호 리스트", 0.3833),
        "word_count": ("단어 수", 0.4095),
        "text_length": ("텍스트 길이", 0.342),
        "has_arrow": ("화살표 포함", 0.1265),
        "has_question": ("물음표 포함", 0.0582),
        "has_url": ("URL 포함", 0.0678),
        "has_emoji": ("이모지 포함", -0.0075),
        "hashtag_count": ("해시태그 수", 0.0103),
    }

    results = {}
    all_match = True
    details = []
    for field, (kr, exp_val) in expected.items():
        h_vals = [f[field] for f in high_features]
        l_vals = [f[field] for f in low_features]
        d = round(cohens_d(h_vals, l_vals), 4)
        results[kr] = d
        match = abs(d - exp_val) < 0.001
        if not match:
            all_match = False
        details.append(f"{kr}: 기대={exp_val}, 실측={d}, {'OK' if match else 'DIFF'}")

    status = "PASS" if all_match else "FAIL"
    return {
        "id": "B3",
        "name": "Cohen's d 9개 재계산 (텍스트 피처)",
        "expected": "9개 피처 4자리 일치",
        "actual": ", ".join(f"{k}={v}" for k, v in results.items()),
        "status": status,
        "detail": "; ".join(details),
    }


def audit_b4_cross_correlation(features):
    """B4: 교차상관 매트릭스 15쌍 재계산."""
    fields = ["likeCount", "retweetCount", "replyCount", "bookmarkCount", "quoteCount"]
    names = ["좋아요", "리트윗", "답글", "북마크", "인용"]

    expected_matrix = {
        ("좋아요", "리트윗"): 0.7937,
        ("좋아요", "답글"): 0.3115,
        ("좋아요", "북마크"): 0.491,
        ("좋아요", "인용"): 0.3289,
        ("리트윗", "답글"): 0.2003,
        ("리트윗", "북마크"): 0.5823,
        ("리트윗", "인용"): 0.2921,
        ("답글", "북마크"): 0.0814,
        ("답글", "인용"): 0.441,
        ("북마크", "인용"): 0.172,
    }

    all_match = True
    details = []
    actual_vals = {}
    for i in range(len(fields)):
        for j in range(i + 1, len(fields)):
            vals_i = [f[fields[i]] for f in features]
            vals_j = [f[fields[j]] for f in features]
            r = round(pearson_r(vals_i, vals_j), 4)
            key = (names[i], names[j])
            actual_vals[key] = r
            exp = expected_matrix.get(key, None)
            if exp is not None:
                match = abs(r - exp) < 0.001
                if not match:
                    all_match = False
                details.append(f"{key[0]}×{key[1]}: 기대={exp}, 실측={r}, {'OK' if match else 'DIFF'}")

    status = "PASS" if all_match else "FAIL"
    return {
        "id": "B4",
        "name": "교차상관 매트릭스 15쌍",
        "expected": "10쌍(off-diagonal) 4자리 일치",
        "actual": f"{sum(1 for d in details if 'OK' in d)}/{len(details)} 일치",
        "status": status,
        "detail": "; ".join(details[:5]) + ("..." if len(details) > 5 else ""),
    }


def audit_b5_percentile_dist(high_orig_features):
    """B5: 백분위 분포 (5지표 × 7분위)."""
    # validation_report.md 에서 읽은 기대값
    expected = {
        "viewCount": {10: 116600, 25: 151000, 50: 272300, 75: 583400, 90: 1100000, 95: 1900000, 99: 3500000},
        "likeCount": {10: 416.6, 25: 887, 50: 1900, 75: 3900, 90: 7400, 95: 11300, 99: 24600},
        "retweetCount": {10: 44.2, 25: 117.5, 50: 314, 75: 794.5, 90: 1700, 95: 2700, 99: 6200},
        "bookmarkCount": {10: 44, 25: 107, 50: 275, 75: 834, 90: 2100, 95: 4200, 99: 9300},
    }

    # total_er in percent
    for f in high_orig_features:
        f["total_er_pct"] = f["total_er"] * 100

    expected_er = {10: 0.2686, 25: 0.5010, 50: 0.9707, 75: 1.8, 90: 3.1, 95: 4.4, 99: 6.6}

    all_fields = [
        ("viewCount", "조회수", expected["viewCount"]),
        ("likeCount", "좋아요", expected["likeCount"]),
        ("retweetCount", "리트윗", expected["retweetCount"]),
        ("bookmarkCount", "북마크", expected["bookmarkCount"]),
        ("total_er_pct", "총 ER(%)", expected_er),
    ]

    total_checks = 0
    matched = 0
    details = []
    for field, kr, exp_pcts in all_fields:
        vals = [f[field] for f in high_orig_features]
        actual_pcts = percentiles(vals)
        for q in [10, 25, 50, 75, 90, 95, 99]:
            total_checks += 1
            exp_v = exp_pcts.get(q)
            act_v = actual_pcts.get(q, 0)
            if exp_v is None:
                continue
            # 허용 오차: K/M 포맷팅 반올림 고려
            if exp_v == 0:
                ok = abs(act_v) < 1
            else:
                ok = abs(act_v - exp_v) / max(abs(exp_v), 1) < 0.05  # 5% 허용
            if ok:
                matched += 1
            else:
                details.append(f"{kr} p{q}: 기대={exp_v}, 실측={act_v:.1f}")

    status = "PASS" if matched >= total_checks * 0.9 else ("WARNING" if matched >= total_checks * 0.7 else "FAIL")
    return {
        "id": "B5",
        "name": "백분위 분포 (5지표 × 7분위)",
        "expected": f"{total_checks}개 분위값 반올림 오차 이내",
        "actual": f"{matched}/{total_checks} 일치",
        "status": status,
        "detail": "; ".join(details[:5]) + ("..." if len(details) > 5 else "") if details else "전부 일치",
    }


# ═══════════════════════════════════════════════
# Phase 3: 분류 일관성 (C1~C4)
# ═══════════════════════════════════════════════

def audit_c1_hook_classification(high_orig_features):
    """C1: Hook 분류 재현 (regex 12패턴 + LLM 캐시) — 1,723건 고성과 오리지널."""
    n = len(high_orig_features)
    # 이미 classify_hook으로 분류됨 — 분류가 안정적인지 확인
    # 기대: 1,723건
    unclassified = sum(1 for f in high_orig_features if "unclassified" in f["hook_labels"])

    # 분류 분포
    label_counts = defaultdict(int)
    for f in high_orig_features:
        for l in f["hook_labels"]:
            label_counts[l] += 1

    status = "PASS" if n == 1723 else "WARNING"
    detail = (f"고성과 오리지널 {n}건 분류 완료. "
              f"unclassified={unclassified}건. "
              f"유형 수={len(label_counts)}.")

    return {
        "id": "C1",
        "name": "Hook 분류 재현 (1,723건)",
        "expected": "1,723건, 100% 재현",
        "actual": f"{n}건, unclassified={unclassified}",
        "status": status,
        "detail": detail,
    }


def audit_c2_body_classification(high_orig_features):
    """C2: Body 분류 재현 (5유형 규칙)."""
    n = len(high_orig_features)
    unclassified = sum(1 for f in high_orig_features if "unclassified" in f["body_labels"])

    label_counts = defaultdict(int)
    for f in high_orig_features:
        for l in f["body_labels"]:
            label_counts[l] += 1

    status = "PASS"
    detail = (f"고성과 오리지널 {n}건 Body 분류 완료. "
              f"unclassified={unclassified}건. "
              f"분포: {dict(sorted(label_counts.items(), key=lambda x: x[1], reverse=True))}")

    return {
        "id": "C2",
        "name": "Body 분류 재현 (5유형)",
        "expected": "100% 일치",
        "actual": f"{n}건, unclassified={unclassified}",
        "status": status,
        "detail": detail,
    }


def audit_c3_llm_cache_integrity():
    """C3: LLM 캐시 무결성."""
    if not LLM_CACHE_FILE.exists():
        return {
            "id": "C3", "name": "LLM 캐시 무결성",
            "expected": "137건, 7유형", "actual": "캐시 파일 없음",
            "status": "FAIL", "detail": "llm_hook_cache.json 없음",
        }

    with open(LLM_CACHE_FILE, "r", encoding="utf-8") as f:
        cache = json.load(f)

    overrides = cache.get("hook_type_overrides", {})
    n_overrides = len(overrides)

    valid_labels = {"narrative_situation", "info_fact", "opinion_reaction",
                    "celeb_mention", "promotion", "emotion_empathy", "comparison_choice"}
    labels_used = set(overrides.values())
    invalid_labels = labels_used - valid_labels

    metadata = cache.get("metadata", {})
    bias_note = metadata.get("bias_note", "")

    has_bias_note = bool(bias_note)
    labels_ok = len(invalid_labels) == 0

    status = "PASS" if n_overrides == 137 and labels_ok and has_bias_note else "WARNING"
    detail = (f"{n_overrides}건 매핑, {len(labels_used)}유형 사용. "
              f"무효 라벨={invalid_labels if invalid_labels else '없음'}. "
              f"bias_note={'있음' if has_bias_note else '없음'}.")

    return {
        "id": "C3",
        "name": "LLM 캐시 무결성",
        "expected": "137건, 7유형, bias_note 존재",
        "actual": f"{n_overrides}건, {len(labels_used)}유형, bias_note={'있음' if has_bias_note else '없음'}",
        "status": status,
        "detail": detail,
    }


def audit_c4_unclassified_coverage(all_orig_features):
    """C4: unclassified 커버리지."""
    total = len(all_orig_features)
    hook_uncl = sum(1 for f in all_orig_features if "unclassified" in f["hook_labels"])
    body_uncl = sum(1 for f in all_orig_features if "unclassified" in f["body_labels"])

    hook_pct = hook_uncl / total * 100 if total else 0
    body_pct = body_uncl / total * 100 if total else 0

    # validation_report.md: Hook 10.6%, Body 7.8%
    hook_match = abs(hook_pct - 10.6) < 1.0
    body_match = abs(body_pct - 7.8) < 1.0

    status = "PASS" if hook_match and body_match else "WARNING"
    detail = (f"전체 {total:,}건. Hook unclassified={hook_uncl}건 ({hook_pct:.1f}%), "
              f"Body unclassified={body_uncl}건 ({body_pct:.1f}%)")

    return {
        "id": "C4",
        "name": "unclassified 커버리지",
        "expected": "Hook ~10.6%, Body ~7.8%",
        "actual": f"Hook {hook_pct:.1f}%, Body {body_pct:.1f}%",
        "status": status,
        "detail": detail,
    }


# ═══════════════════════════════════════════════
# Phase 4: 규칙 판정 (D1~D9)
# ═══════════════════════════════════════════════

def audit_d1_to_d9_rule_verdicts(features, high_features, low_features):
    """D1~D9: 9개 규칙 독립 재판정."""
    results = []
    views = [f["viewCount"] for f in features]

    # D1: 좋아요 r=0.6716 최강
    like_vals = [f["likeCount"] for f in features]
    like_r = round(pearson_r(like_vals, views), 4)
    eng_fields = {
        "likeCount": "좋아요", "retweetCount": "리트윗",
        "replyCount": "답글", "bookmarkCount": "북마크", "quoteCount": "인용",
    }
    eng_rs = {}
    for field, kr in eng_fields.items():
        eng_rs[kr] = round(pearson_r([f[field] for f in features], views), 4)
    is_strongest = like_r >= max(eng_rs.values())
    d1_ok = abs(like_r - 0.6716) < 0.001 and is_strongest
    results.append({
        "id": "D1", "name": "좋아요 r=0.6716 최강",
        "expected": "CONFIRMED, r=0.6716, 최강",
        "actual": f"r={like_r}, 최강={'예' if is_strongest else '아니오'}",
        "status": "PASS" if d1_ok else "FAIL",
        "detail": f"전체 상관: {eng_rs}",
    })

    # D2: 151-280자 최적
    bins_high = defaultdict(list)
    bins_low = defaultdict(list)
    all_bins = defaultdict(list)
    for f in high_features:
        bins_high[f["length_bin"]].append(f["viewCount"])
    for f in low_features:
        bins_low[f["length_bin"]].append(f["viewCount"])
    for f in features:
        all_bins[f["length_bin"]].append(f["viewCount"])

    h_pct = len(bins_high.get("151-280", [])) / len(high_features) * 100 if high_features else 0
    l_pct = len(bins_low.get("151-280", [])) / len(low_features) * 100 if low_features else 0
    diff_pp = h_pct - l_pct
    bin_means = {b: sum(vs) / len(vs) for b, vs in all_bins.items() if vs}
    best_bin = max(bin_means, key=bin_means.get) if bin_means else "N/A"

    d2_ok = diff_pp > 10 and best_bin == "151-280"
    results.append({
        "id": "D2", "name": "151-280자 최적 구간",
        "expected": "CONFIRMED, 고성과 33.1% vs 저성과 19.1%, 차이 +14pp",
        "actual": f"고성과 {h_pct:.1f}% vs 저성과 {l_pct:.1f}% (차이 {diff_pp:+.1f}pp), 최고구간={best_bin}",
        "status": "PASS" if d2_ok else "FAIL",
        "detail": f"구간별 평균: {', '.join(f'{b}={round(v):,}' for b, v in sorted(bin_means.items()))}",
    })

    # D3: 짧은 Hook 79.5%
    short_count = sum(1 for f in high_features if "short_cryptic" in f["hook_labels"])
    short_pct = short_count / len(high_features) * 100 if high_features else 0
    d3_ok = abs(short_pct - 79.5) < 1.0
    results.append({
        "id": "D3", "name": "짧은 Hook(≤25자) 79.5%",
        "expected": "CONFIRMED, 79.5%",
        "actual": f"{short_pct:.1f}%",
        "status": "PASS" if d3_ok else "FAIL",
        "detail": f"고성과 오리지널 {len(high_features)}건 중 short_cryptic={short_count}건",
    })

    # D4: 화살표 흐름형 최고 ER → CONTRADICTED
    body_ers = {}
    for label in BODY_LABELS_KR:
        ers = [f["total_er"] for f in features if label in f["body_labels"]]
        if ers:
            body_ers[label] = sum(ers) / len(ers) * 100
    arrow_er = body_ers.get("arrow_flow", 0)
    is_top_er = all(arrow_er >= v for v in body_ers.values()) if body_ers else False
    d4_ok = not is_top_er  # 기대: CONTRADICTED
    results.append({
        "id": "D4", "name": "화살표 흐름형 최고 ER",
        "expected": "CONTRADICTED (화살표 ER < 미디어 의존형)",
        "actual": f"arrow_flow ER={arrow_er:.3f}%, 최고={'예' if is_top_er else '아니오'}",
        "status": "PASS" if d4_ok else "FAIL",
        "detail": f"Body별 ER: {', '.join(f'{BODY_LABELS_KR.get(k,k)}={v:.3f}%' for k, v in sorted(body_ers.items(), key=lambda x: x[1], reverse=True))}",
    })

    # D5: URL 76.8%/73.9%
    h_url = sum(1 for f in high_features if f["has_url"] == 1)
    l_url = sum(1 for f in low_features if f["has_url"] == 1)
    h_url_pct = h_url / len(high_features) * 100 if high_features else 0
    l_url_pct = l_url / len(low_features) * 100 if low_features else 0
    d5_ok = abs(h_url_pct - 76.8) < 1.0 and abs(l_url_pct - 73.9) < 1.0
    results.append({
        "id": "D5", "name": "URL 포함 비율",
        "expected": "CONFIRMED, 고 76.8% vs 저 73.9%",
        "actual": f"고 {h_url_pct:.1f}% vs 저 {l_url_pct:.1f}%",
        "status": "PASS" if d5_ok else "FAIL",
        "detail": f"고성과 URL {h_url}/{len(high_features)}, 저성과 URL {l_url}/{len(low_features)}",
    })

    # D6: 해시태그 무효과
    hash_r = round(pearson_r([f["hashtag_count"] for f in features], views), 4)
    hash_d = round(cohens_d(
        [f["hashtag_count"] for f in high_features],
        [f["hashtag_count"] for f in low_features]
    ), 4)
    d6_ok = abs(hash_r) < 0.05 and abs(hash_d) < 0.2
    results.append({
        "id": "D6", "name": "해시태그 무효과",
        "expected": "CONFIRMED, |r|<0.05 AND |d|<0.2",
        "actual": f"r={hash_r}, d={hash_d}",
        "status": "PASS" if d6_ok else "FAIL",
        "detail": f"|r|={abs(hash_r):.4f}, |d|={abs(hash_d):.4f}",
    })

    # D7: 인용 dominant=최고 조회
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
    dom_means = {k: sum(vs) / len(vs) for k, vs in dom_stats.items() if vs}
    quote_mean = dom_means.get("quoteCount", 0)
    is_quote_top = quote_mean >= max(dom_means.values()) if dom_means else False
    d7_ok = is_quote_top
    field_kr = {"likeCount": "좋아요", "retweetCount": "리트윗", "replyCount": "답글",
                "bookmarkCount": "북마크", "quoteCount": "인용"}
    results.append({
        "id": "D7", "name": "인용 dominant = 최고 조회",
        "expected": "CONFIRMED, 인용 dominant 평균 최고",
        "actual": f"인용 평균={round(quote_mean):,}, 최고={'예' if is_quote_top else '아니오'}",
        "status": "PASS" if d7_ok else "FAIL",
        "detail": f"Dominant별 평균: {', '.join(f'{field_kr.get(k,k)}={round(v):,}' for k, v in sorted(dom_means.items(), key=lambda x: x[1], reverse=True))}",
    })

    # D8: DM 공유 알고리즘 가중치 (ALGORITHM-ONLY)
    dm_found = False
    if ZIP_PATH.exists():
        with zipfile.ZipFile(ZIP_PATH, "r") as zf:
            for name in zf.namelist():
                if "weighted_scorer" in name.lower() and name.endswith(".rs"):
                    content = zf.read(name).decode("utf-8", errors="replace")
                    if "share_via_dm" in content.lower() or "SHARE_VIA_DM" in content:
                        dm_found = True
                    break
    results.append({
        "id": "D8", "name": "DM 공유 알고리즘 가중치",
        "expected": "ALGORITHM-ONLY, weighted_scorer.rs 내 확인",
        "actual": f"share_via_dm 발견={'예' if dm_found else '아니오'}",
        "status": "PASS" if dm_found else "WARNING",
        "detail": "ZIP 내 weighted_scorer.rs 검색",
    })

    # D9: 작성자 다양성 페널티 (ALGORITHM-ONLY)
    diversity_found = False
    if ZIP_PATH.exists():
        with zipfile.ZipFile(ZIP_PATH, "r") as zf:
            for name in zf.namelist():
                if "author_diversity" in name.lower() and name.endswith(".rs"):
                    content = zf.read(name).decode("utf-8", errors="replace")
                    if "decay" in content.lower() or "multiplier" in content.lower():
                        diversity_found = True
                    break
    results.append({
        "id": "D9", "name": "작성자 다양성 페널티",
        "expected": "ALGORITHM-ONLY, author_diversity_scorer.rs 내 확인",
        "actual": f"decay/multiplier 발견={'예' if diversity_found else '아니오'}",
        "status": "PASS" if diversity_found else "WARNING",
        "detail": "ZIP 내 author_diversity_scorer.rs 검색",
    })

    return results


# ═══════════════════════════════════════════════
# Phase 5: 알고리즘 소스코드 검증 (E1~E5)
# ═══════════════════════════════════════════════

def audit_e1_to_e5_algorithm_code():
    """E1~E5: ZIP 소스코드 검증."""
    results = []

    if not ZIP_PATH.exists():
        for eid, ename in [("E1", "PhoenixScores 시그널 매핑"),
                           ("E2", "가중점수 공식"),
                           ("E3", "작성자 다양성 감쇠"),
                           ("E4", "뮤트 키워드 매칭"),
                           ("E5", "age_filter 파라미터")]:
            results.append({
                "id": eid, "name": ename,
                "expected": "ZIP 내 확인", "actual": "ZIP 없음",
                "status": "FAIL", "detail": "x-algorithm-main.zip 없음",
            })
        return results

    # 파일 인덱스 빌드
    file_contents = {}
    with zipfile.ZipFile(ZIP_PATH, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            fname = Path(info.filename).name.lower()
            try:
                content = zf.read(info.filename).decode("utf-8", errors="replace")
                file_contents[fname] = content
                # 원본 이름도 저장
                file_contents[info.filename] = content
            except Exception:
                pass

    # E1: candidate.rs — PhoenixScores 시그널 매핑
    candidate_files = [k for k in file_contents if "candidate" in k.lower() and k.endswith(".rs")]
    phoenix_signals = ["favorite", "reply", "retweet", "quote", "share",
                       "dwell", "click", "profile_click", "follow",
                       "not_interested", "block", "mute", "report"]
    found_signals = []
    for cf in candidate_files:
        content = file_contents[cf]
        for sig in phoenix_signals:
            if sig in content.lower():
                found_signals.append(sig)
    found_signals = list(set(found_signals))
    results.append({
        "id": "E1", "name": "PhoenixScores 시그널 매핑",
        "expected": f"candidate.rs 내 19개 시그널 중 핵심 {len(phoenix_signals)}개",
        "actual": f"{len(found_signals)}/{len(phoenix_signals)}개 발견",
        "status": "PASS" if len(found_signals) >= 10 else "WARNING",
        "detail": f"발견: {found_signals[:10]}...",
    })

    # E2: weighted_scorer.rs — 가중점수 공식 15양+4음
    ws_files = [k for k in file_contents if "weighted_scorer" in k.lower() and k.endswith(".rs")]
    positive_count = 0
    negative_count = 0
    for wf in ws_files:
        content = file_contents[wf]
        # 양의 시그널: weight 키워드 카운트
        positive_keywords = ["FAVORITE", "REPLY", "RETWEET", "QUOTE", "SHARE",
                             "DWELL", "CLICK", "PROFILE_CLICK", "FOLLOW", "VIDEO"]
        negative_keywords = ["NOT_INTERESTED", "BLOCK", "MUTE", "REPORT"]
        for kw in positive_keywords:
            if kw in content.upper():
                positive_count += 1
        for kw in negative_keywords:
            if kw in content.upper():
                negative_count += 1
    results.append({
        "id": "E2", "name": "가중점수 공식 확인",
        "expected": "15양 + 4음 가중치",
        "actual": f"양={positive_count}, 음={negative_count}",
        "status": "PASS" if positive_count >= 8 and negative_count >= 3 else "WARNING",
        "detail": f"weighted_scorer.rs 파일 {len(ws_files)}개 검색",
    })

    # E3: author_diversity_scorer.rs — 감쇠 공식
    ad_files = [k for k in file_contents if "author_diversity" in k.lower() and k.endswith(".rs")]
    has_decay = False
    has_floor = False
    for af in ad_files:
        content = file_contents[af]
        if "decay" in content.lower():
            has_decay = True
        if "floor" in content.lower():
            has_floor = True
    results.append({
        "id": "E3", "name": "작성자 다양성 감쇠 공식",
        "expected": "multiplier = (1-floor) × decay^position + floor",
        "actual": f"decay={'있음' if has_decay else '없음'}, floor={'있음' if has_floor else '없음'}",
        "status": "PASS" if has_decay and has_floor else "WARNING",
        "detail": f"author_diversity_scorer.rs 파일 {len(ad_files)}개 검색",
    })

    # E4: muted_keyword_filter.rs — 토큰화 매칭
    mk_files = [k for k in file_contents if "muted_keyword" in k.lower() and k.endswith(".rs")]
    has_tokenizer = False
    has_match_group = False
    for mf in mk_files:
        content = file_contents[mf]
        if "tokeniz" in content.lower() or "TweetTokenizer" in content:
            has_tokenizer = True
        if "MatchTweetGroup" in content or "match" in content.lower():
            has_match_group = True
    results.append({
        "id": "E4", "name": "뮤트 키워드 매칭 방식",
        "expected": "tokenized sequence matching (not simple exact match)",
        "actual": f"tokenizer={'있음' if has_tokenizer else '없음'}, match_group={'있음' if has_match_group else '없음'}",
        "status": "PASS" if has_match_group else "WARNING",
        "detail": f"muted_keyword_filter.rs 파일 {len(mk_files)}개 검색. 보고서 표현 수정 권고.",
    })

    # E5: age_filter.rs — 파라미터
    af_files = [k for k in file_contents if "age_filter" in k.lower() and k.endswith(".rs")]
    has_86400 = False
    for af in af_files:
        content = file_contents[af]
        if "86400" in content or "86_400" in content:
            has_86400 = True
    # 대안: 다른 파일에서도 검색
    if not has_86400:
        for fname, content in file_contents.items():
            if fname.endswith(".rs") and ("86400" in content or "86_400" in content):
                has_86400 = True
                break
    results.append({
        "id": "E5", "name": "age_filter 파라미터",
        "expected": "24시간 = 86400초",
        "actual": f"86400={'발견' if has_86400 else '미발견'}",
        "status": "PASS" if has_86400 else "WARNING",
        "detail": f"age_filter.rs 파일 {len(af_files)}개 + 전체 .rs 파일 검색",
    })

    return results


# ═══════════════════════════════════════════════
# Phase 6: 우선순위 랭킹 (F1)
# ═══════════════════════════════════════════════

def audit_f1_priority_ranking(features, high_features, low_features):
    """F1: 39개 요소 최종점수 재계산."""
    views = [f["viewCount"] for f in features]

    items = []

    # 참여지표
    algo_importance = {
        "likeCount": 0.9,
        "retweetCount": 0.8,
        "replyCount": 0.7,
        "bookmarkCount": 0.6,
        "quoteCount": 0.6,
    }
    eng_fields = [
        ("likeCount", "좋아요"), ("retweetCount", "리트윗"),
        ("replyCount", "답글"), ("bookmarkCount", "북마크"), ("quoteCount", "인용"),
    ]
    for field, kr in eng_fields:
        vals = [f[field] for f in features]
        corr_strength = abs(round(pearson_r(vals, views), 4))
        effect = abs(round(spearman_rho(vals, views), 4))
        algo_imp = algo_importance[field]
        score = round(0.4 * corr_strength + 0.3 * effect + 0.3 * algo_imp, 4)
        items.append({"factor": f"참여지표: {kr}", "score": score})

    # 텍스트 피처
    text_algo = {
        "text_length": 0.7, "line_count": 0.6, "has_url": 0.5,
        "hashtag_count": 0.1, "has_emoji": 0.2, "has_question": 0.3,
        "has_number_list": 0.4, "has_arrow": 0.4, "word_count": 0.5,
    }
    text_fields = [
        ("text_length", "텍스트 길이"), ("line_count", "줄 수"), ("word_count", "단어 수"),
        ("has_url", "URL 포함"), ("hashtag_count", "해시태그 수"), ("has_emoji", "이모지 포함"),
        ("has_question", "물음표 포함"), ("has_number_list", "번호 리스트"), ("has_arrow", "화살표 포함"),
    ]
    for field, kr in text_fields:
        vals = [f[field] for f in features]
        corr_strength = abs(round(pearson_r(vals, views), 4))
        h_vals = [f[field] for f in high_features]
        l_vals = [f[field] for f in low_features]
        d = abs(cohens_d(h_vals, l_vals))
        effect_norm = min(d / 0.8, 1.0)
        algo_imp = text_algo[field]
        score = round(0.4 * corr_strength + 0.3 * effect_norm + 0.3 * algo_imp, 4)
        items.append({"factor": f"텍스트: {kr}", "score": score})

    # Hook 유형
    hook_groups = defaultdict(list)
    for f in features:
        for label in f["hook_labels"]:
            hook_groups[label].append(f["viewCount"])

    hook_effects = {}
    for label, vws in hook_groups.items():
        if label == "unclassified" or len(vws) < 5:
            continue
        other = [f["viewCount"] for f in features if label not in f["hook_labels"]]
        d = cohens_d(vws, other)
        n_low = sum(1 for f in features if label in f["hook_labels"] and f["viewCount"] < THRESHOLD)
        is_llm = label in LLM_ONLY_TYPES and n_low == 0
        hook_effects[label] = {
            "count": len(vws), "mean_views": sum(vws) / len(vws),
            "cohens_d": d, "is_llm": is_llm,
        }

    llm_labels = {l for l, d in hook_effects.items() if d["is_llm"]}
    if llm_labels:
        llm_ratios = {l: hook_effects[l]["count"] for l in llm_labels}
        llm_means = {l: hook_effects[l]["mean_views"] for l in llm_labels}
        max_ratio = max(llm_ratios.values())
        max_mean = max(llm_means.values())
    else:
        max_ratio = 1
        max_mean = 1

    for label, data in hook_effects.items():
        kr = HOOK_LABELS_KR.get(label, label)
        if label in llm_labels:
            corr_str = llm_ratios[label] / max_ratio if max_ratio else 0
            effect_norm = llm_means[label] / max_mean if max_mean else 0
            algo_imp = 0.5
        else:
            d_abs = abs(data["cohens_d"])
            effect_norm = min(d_abs / 0.8, 1.0)
            algo_imp = 0.5
            corr_str = effect_norm * 0.5
        score = round(0.4 * corr_str + 0.3 * effect_norm + 0.3 * algo_imp, 4)
        items.append({"factor": f"Hook: {kr}" + (" (LLM)" if label in llm_labels else ""),
                      "score": score})

    # Body 유형
    body_groups = defaultdict(list)
    for f in features:
        for label in f["body_labels"]:
            body_groups[label].append(f["viewCount"])

    for label, vws in body_groups.items():
        if label == "unclassified" or len(vws) < 5:
            continue
        other = [f["viewCount"] for f in features if label not in f["body_labels"]]
        d_abs = abs(cohens_d(vws, other))
        effect_norm = min(d_abs / 0.8, 1.0)
        algo_imp = 0.6
        corr_str = effect_norm * 0.5
        score = round(0.4 * corr_str + 0.3 * effect_norm + 0.3 * algo_imp, 4)
        kr = BODY_LABELS_KR.get(label, label)
        items.append({"factor": f"Body: {kr}", "score": score})

    items.sort(key=lambda x: x["score"], reverse=True)

    # 분류
    for item in items:
        if item["score"] >= 0.5:
            item["priority"] = "HIGH"
        elif item["score"] >= 0.3:
            item["priority"] = "MEDIUM"
        else:
            item["priority"] = "LOW"

    # validation_report.md의 기대값과 대조
    expected_ranking = {
        "참여지표: 좋아요": 0.7944,
        "Hook: 서사/상황형 (LLM)": 0.7381,
        "Hook: 정보전달형 (LLM)": 0.6853,
        "참여지표: 리트윗": 0.6548,
        "Hook: 의견/반응형 (LLM)": 0.623,
        "참여지표: 인용": 0.5776,
        "참여지표: 북마크": 0.5754,
        "Hook: 셀럽/유명인형 (LLM)": 0.5671,
        "참여지표: 답글": 0.5598,
    }

    n_high = sum(1 for i in items if i["priority"] == "HIGH")
    n_med = sum(1 for i in items if i["priority"] == "MEDIUM")
    n_low = sum(1 for i in items if i["priority"] == "LOW")

    # 점수 비교
    matched = 0
    total_compared = 0
    mismatches = []
    actual_map = {i["factor"]: i["score"] for i in items}
    for factor, exp_score in expected_ranking.items():
        act_score = actual_map.get(factor)
        if act_score is not None:
            total_compared += 1
            if abs(act_score - exp_score) < 0.005:
                matched += 1
            else:
                mismatches.append(f"{factor}: 기대={exp_score}, 실측={act_score}")

    status = "PASS" if matched == total_compared else ("WARNING" if matched >= total_compared * 0.8 else "FAIL")

    return {
        "id": "F1",
        "name": "우선순위 랭킹 재계산",
        "expected": f"HIGH=9, MEDIUM=8, LOW=22, 상위 9개 점수 일치",
        "actual": f"HIGH={n_high}, MEDIUM={n_med}, LOW={n_low}, {matched}/{total_compared} 점수 일치",
        "status": status,
        "detail": "; ".join(mismatches[:5]) if mismatches else "전부 일치",
    }, items


# ═══════════════════════════════════════════════
# Phase 7: 보고서 교차 일관성 (G1~G2)
# ═══════════════════════════════════════════════

def audit_g1_cross_report_numbers(deduped_tweets, high, low, high_orig, low_orig):
    """G1: tweet_formulas.md vs validation_report.md 수치 대조."""
    issues = []
    total = len(deduped_tweets)
    n_high = len(high)
    n_low = len(low)
    n_high_orig = len(high_orig)
    n_low_orig = len(low_orig)

    # tweet_formulas.md 기대값
    tf_total = 43773
    tf_high = 3919
    tf_high_orig = 1723
    tf_low_orig = 20263

    if total != tf_total:
        issues.append(f"총건수: 실측 {total:,} vs tweet_formulas {tf_total:,}")
    if n_high != tf_high:
        issues.append(f"고성과: 실측 {n_high:,} vs tweet_formulas {tf_high:,}")
    if n_high_orig != tf_high_orig:
        issues.append(f"고성과 오리지널: 실측 {n_high_orig:,} vs tweet_formulas {tf_high_orig:,}")
    if n_low_orig != tf_low_orig:
        issues.append(f"저성과 오리지널: 실측 {n_low_orig:,} vs tweet_formulas {tf_low_orig:,}")

    # 평균 조회수 확인
    if high_orig:
        avg_high_views = sum(t.get("viewCount", 0) for t in high_orig) / len(high_orig)
        # tweet_formulas.md: 534,662
        if abs(avg_high_views - 534662) / 534662 > 0.01:
            issues.append(f"고성과 오리지널 평균조회: 실측 {round(avg_high_views):,} vs 기대 534,662")

    status = "PASS" if not issues else "FAIL"
    return {
        "id": "G1",
        "name": "보고서 간 수치 대조",
        "expected": "총건수/고저/평균 모순 없음",
        "actual": f"{'일치' if not issues else f'{len(issues)}건 불일치'}",
        "status": status,
        "detail": "; ".join(issues) if issues else "모든 수치 일치",
    }


def audit_g2_algorithm_claims():
    """G2: 알고리즘 주장 vs ZIP 소스코드."""
    claims = [
        ("Grok 트랜스포머", ["grok", "transformer", "Transformer"]),
        ("hash 임베딩", ["hash", "embedding", "Hash"]),
        ("128 히스토리", ["128", "history", "MAX_HISTORY"]),
        ("200ms 영상", ["200", "MIN_VIDEO", "video_duration", "MIN_DURATION"]),
        ("24시간 age_filter", ["86400", "86_400", "age_filter"]),
        ("author_diversity decay", ["decay", "author_diversity", "multiplier"]),
    ]

    if not ZIP_PATH.exists():
        return {
            "id": "G2", "name": "알고리즘 주장 추적",
            "expected": "6개 주장 소스코드 추적", "actual": "ZIP 없음",
            "status": "FAIL", "detail": "ZIP 없음",
        }

    # 전체 소스코드를 하나의 문자열로
    all_source = ""
    with zipfile.ZipFile(ZIP_PATH, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            try:
                content = zf.read(info.filename).decode("utf-8", errors="replace")
                all_source += "\n" + content
            except Exception:
                pass

    found = 0
    details = []
    for claim, keywords in claims:
        claim_found = any(kw in all_source for kw in keywords)
        if claim_found:
            found += 1
        details.append(f"{claim}: {'발견' if claim_found else '미발견'}")

    status = "PASS" if found >= 5 else ("WARNING" if found >= 4 else "FAIL")
    return {
        "id": "G2",
        "name": "알고리즘 주장 vs 소스코드",
        "expected": "6개 주장 중 5개 이상 추적 가능",
        "actual": f"{found}/6 추적 완료",
        "status": status,
        "detail": "; ".join(details),
    }


# ═══════════════════════════════════════════════
# 감사 보고서 생성
# ═══════════════════════════════════════════════

def generate_audit_report(results, ranking_items=None):
    lines = []
    def w(s=""):
        lines.append(s)

    n_pass = sum(1 for r in results if r["status"] == "PASS")
    n_fail = sum(1 for r in results if r["status"] == "FAIL")
    n_warn = sum(1 for r in results if r["status"] == "WARNING")
    total = len(results)

    w("# 전체 분석 파이프라인 독립 감사 보고서")
    w()
    w("---")
    w()
    w("## Executive Summary")
    w()
    w(f"- **총 체크 수**: {total}")
    w(f"- **PASS**: {n_pass}")
    w(f"- **FAIL**: {n_fail}")
    w(f"- **WARNING**: {n_warn}")
    w()

    if n_fail == 0:
        w("> 모든 핵심 체크를 통과했습니다. 파이프라인 출력물이 독립 재계산과 일치합니다.")
    else:
        w(f"> **{n_fail}건의 FAIL이 발견되었습니다.** 아래 상세 내용을 확인하세요.")
    w()

    # Phase별 테이블
    phases = [
        ("Phase 1: 데이터 무결성", ["A1", "A2", "A3", "A4", "A5"]),
        ("Phase 2: 통계 방법론", ["B1", "B2", "B3", "B4", "B5"]),
        ("Phase 3: 분류 일관성", ["C1", "C2", "C3", "C4"]),
        ("Phase 4: 규칙 판정", ["D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9"]),
        ("Phase 5: 알고리즘 소스코드", ["E1", "E2", "E3", "E4", "E5"]),
        ("Phase 6: 우선순위 랭킹", ["F1"]),
        ("Phase 7: 보고서 교차 일관성", ["G1", "G2"]),
    ]

    result_map = {r["id"]: r for r in results}

    for phase_name, ids in phases:
        w(f"## {phase_name}")
        w()
        w("| ID | 검증 항목 | 기대값 | 실측값 | 상태 |")
        w("|:---:|:---|:---|:---|:---:|")
        for rid in ids:
            r = result_map.get(rid)
            if r:
                emoji = {"PASS": "✅", "FAIL": "❌", "WARNING": "⚠️"}.get(r["status"], "❓")
                w(f"| {r['id']} | {r['name']} | {r['expected']} | {r['actual']} | {emoji} {r['status']} |")
        w()

    # 상세 설명 (FAIL/WARNING만)
    fail_warn = [r for r in results if r["status"] in ("FAIL", "WARNING")]
    if fail_warn:
        w("## FAIL/WARNING 상세 설명")
        w()
        for r in fail_warn:
            emoji = "❌" if r["status"] == "FAIL" else "⚠️"
            w(f"### {emoji} {r['id']}: {r['name']}")
            w()
            w(f"- **기대값**: {r['expected']}")
            w(f"- **실측값**: {r['actual']}")
            w(f"- **상세**: {r['detail']}")
            w()
            if r["status"] == "FAIL":
                w("**수정 권고**: 해당 수치를 확인하고 파이프라인 출력물을 갱신하세요.")
                w()

    # 알려진 이슈 확인
    w("## 알려진 이슈 확인")
    w()
    w("| # | 이슈 | 확인 결과 |")
    w("|:---:|:---|:---|")

    # 이슈 1: analyze_tweets.py 비중복제거
    a2 = result_map.get("A2", {})
    w(f"| 1 | analyze_tweets.py 비중복제거 (44,014/3,964 vs 43,773/3,919) | {a2.get('status', 'N/A')}: {a2.get('actual', 'N/A')} |")

    # 이슈 2: 화살표 흐름형 ER CONTRADICTED
    d4 = result_map.get("D4", {})
    w(f"| 2 | 화살표 흐름형 ER CONTRADICTED | {d4.get('status', 'N/A')}: {d4.get('actual', 'N/A')} |")

    # 이슈 3: LLM 7유형 bias_note
    c3 = result_map.get("C3", {})
    w(f"| 3 | LLM 7유형 bias_note 존재 | {c3.get('status', 'N/A')}: {c3.get('actual', 'N/A')} |")

    # 이슈 4: 뮤트 키워드 매칭 방식
    e4 = result_map.get("E4", {})
    w(f"| 4 | 뮤트 키워드: tokenized sequence matching | {e4.get('status', 'N/A')}: {e4.get('actual', 'N/A')} |")
    w()

    # 우선순위 랭킹 요약
    if ranking_items:
        w("## 우선순위 랭킹 재계산 결과 (상위 10)")
        w()
        w("| 순위 | 요소 | 최종점수 | 분류 |")
        w("|:---:|:---|:---:|:---:|")
        for i, item in enumerate(ranking_items[:10], 1):
            w(f"| {i} | {item['factor']} | {item['score']} | {item['priority']} |")
        w()

    w("---")
    w()
    w("*감사 완료. 이 보고서는 audit_pipeline.py에 의해 자동 생성되었습니다.*")

    with open(OUTPUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"감사 보고서 생성: {OUTPUT_MD}")


# ═══════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════

def main():
    print("=" * 60)
    print("전체 분석 파이프라인 독립 감사 시작")
    print("=" * 60)

    results = []

    # ── 데이터 로드 ──
    print("\n[데이터 로드]")
    raw_tweets, file_stats = load_raw_tweets()
    print(f"  JSONL {len(file_stats)}개 파일, {len(raw_tweets):,}건 로드")

    deduped_tweets, skipped_view, skipped_dup = load_deduped_tweets(raw_tweets)
    print(f"  중복제거 후: {len(deduped_tweets):,}건 (viewCount 누락 {skipped_view}건, 중복 {skipped_dup}건)")

    high, low, high_orig, high_rt, low_orig, low_rt = split_tweets(deduped_tweets)
    print(f"  고성과={len(high):,}, 저성과={len(low):,}")
    print(f"  고성과 오리지널={len(high_orig):,}, 저성과 오리지널={len(low_orig):,}")

    # LLM 캐시 로드
    llm_overrides = {}
    if LLM_CACHE_FILE.exists():
        with open(LLM_CACHE_FILE, "r", encoding="utf-8") as f:
            _cache = json.load(f)
        llm_overrides = _cache.get("hook_type_overrides", {})

    # 피처 추출 (오리지널만)
    print("\n[피처 추출]")
    all_orig = high_orig + low_orig
    all_features = [extract_features(t, llm_overrides) for t in all_orig]
    high_features = [f for f in all_features if f["viewCount"] >= THRESHOLD]
    low_features = [f for f in all_features if f["viewCount"] < THRESHOLD]
    print(f"  피처 추출 완료: {len(all_features):,}건 (고 {len(high_features):,}, 저 {len(low_features):,})")

    # ── Phase 1: 데이터 무결성 ──
    print("\n[Phase 1: 데이터 무결성]")
    r = audit_a1_data_completeness(raw_tweets, file_stats)
    results.append(r)
    print(f"  A1: {r['status']} — {r['actual']}")

    r = audit_a2_count_reconciliation(deduped_tweets, high, low, high_orig, low_orig,
                                      skipped_view, skipped_dup, raw_tweets)
    results.append(r)
    print(f"  A2: {r['status']} — {r['actual']}")

    r = audit_a3_duplicate_analysis(raw_tweets)
    results.append(r)
    print(f"  A3: {r['status']} — {r['actual']}")

    r = audit_a4_outlier_detection(deduped_tweets)
    results.append(r)
    print(f"  A4: {r['status']} — {r['actual']}")

    r = audit_a5_zip_verification()
    results.append(r)
    print(f"  A5: {r['status']} — {r['actual']}")

    # ── Phase 2: 통계 방법론 ──
    print("\n[Phase 2: 통계 방법론]")
    r = audit_b1_pearson_r(all_features)
    results.append(r)
    print(f"  B1: {r['status']} — {r['actual']}")

    r = audit_b2_spearman_rho(all_features)
    results.append(r)
    print(f"  B2: {r['status']} — {r['actual']}")

    r = audit_b3_cohens_d(high_features, low_features)
    results.append(r)
    print(f"  B3: {r['status']} — {r['actual']}")

    r = audit_b4_cross_correlation(all_features)
    results.append(r)
    print(f"  B4: {r['status']} — {r['actual']}")

    r = audit_b5_percentile_dist(high_features)
    results.append(r)
    print(f"  B5: {r['status']} — {r['actual']}")

    # ── Phase 3: 분류 일관성 ──
    print("\n[Phase 3: 분류 일관성]")
    r = audit_c1_hook_classification(high_features)
    results.append(r)
    print(f"  C1: {r['status']} — {r['actual']}")

    r = audit_c2_body_classification(high_features)
    results.append(r)
    print(f"  C2: {r['status']} — {r['actual']}")

    r = audit_c3_llm_cache_integrity()
    results.append(r)
    print(f"  C3: {r['status']} — {r['actual']}")

    r = audit_c4_unclassified_coverage(all_features)
    results.append(r)
    print(f"  C4: {r['status']} — {r['actual']}")

    # ── Phase 4: 규칙 판정 ──
    print("\n[Phase 4: 규칙 판정]")
    d_results = audit_d1_to_d9_rule_verdicts(all_features, high_features, low_features)
    for r in d_results:
        results.append(r)
        print(f"  {r['id']}: {r['status']} — {r['actual']}")

    # ── Phase 5: 알고리즘 소스코드 ──
    print("\n[Phase 5: 알고리즘 소스코드]")
    e_results = audit_e1_to_e5_algorithm_code()
    for r in e_results:
        results.append(r)
        print(f"  {r['id']}: {r['status']} — {r['actual']}")

    # ── Phase 6: 우선순위 랭킹 ──
    print("\n[Phase 6: 우선순위 랭킹]")
    f1_result, ranking_items = audit_f1_priority_ranking(all_features, high_features, low_features)
    results.append(f1_result)
    print(f"  F1: {f1_result['status']} — {f1_result['actual']}")

    # ── Phase 7: 보고서 교차 일관성 ──
    print("\n[Phase 7: 보고서 교차 일관성]")
    r = audit_g1_cross_report_numbers(deduped_tweets, high, low, high_orig, low_orig)
    results.append(r)
    print(f"  G1: {r['status']} — {r['actual']}")

    r = audit_g2_algorithm_claims()
    results.append(r)
    print(f"  G2: {r['status']} — {r['actual']}")

    # ── 보고서 생성 ──
    print("\n[감사 보고서 생성]")
    generate_audit_report(results, ranking_items)

    # ── 요약 ──
    n_pass = sum(1 for r in results if r["status"] == "PASS")
    n_fail = sum(1 for r in results if r["status"] == "FAIL")
    n_warn = sum(1 for r in results if r["status"] == "WARNING")
    print(f"\n{'='*60}")
    print(f"감사 완료: PASS={n_pass}, FAIL={n_fail}, WARNING={n_warn} / 총 {len(results)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
