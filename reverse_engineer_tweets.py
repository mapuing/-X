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
THRESHOLD = 100_000

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
    "short_cryptic": "짧은 임팩트형(≤25자)",
}


def extract_hook(text):
    """첫 번째 비어있지 않은 줄을 Hook으로 추출, URL 제거."""
    text_clean = URL_RE.sub("", text).strip()
    for line in text_clean.split("\n"):
        line = line.strip()
        if line:
            return line
    return text_clean


def classify_hook(hook_text):
    """Hook 텍스트를 다중 레이블로 분류."""
    labels = []
    for name, pattern in HOOK_PATTERNS.items():
        if name == "short_cryptic":
            if len(hook_text) <= 25:
                labels.append(name)
        elif pattern and pattern.search(hook_text):
            labels.append(name)
    if not labels:
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

        results.append({
            "text": text,
            "hook_text": hook_text,
            "hook_labels": hook_labels,
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

        # 고/저 비율 계산 (저성과 카운트가 0이면 높은 값 부여)
        if l_count > 0:
            # 비율 정규화: 고성과 트윗 중 비율 / 저성과 트윗 중 비율은 아니고
            # 단순 고성과 개수 / 저성과 개수로 비율 계산
            ratio = h_count / l_count
        else:
            ratio = h_count  # 저성과에 없으면 고성과 개수 자체를 비율로

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
                      account_high_stats):
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

    # ── 6. 종합 체크리스트 ──
    w("## 6. 종합 트윗 작성 공식 & 체크리스트")
    w()
    w("### 황금 공식")
    w()
    w("```")
    w("[강력한 Hook] + [구조화된 Body] + [미디어/이미지] = 바이럴 트윗")
    w("```")
    w()

    w("### Hook 작성 체크리스트")
    w()
    w("- [ ] 첫 줄에 **독자를 지정**했는가? (\"~하는 사람\", \"~인 분\")")
    w("- [ ] **권위/신뢰 요소**가 포함되었는가? (현직, 전문, N년차)")
    w("- [ ] **숫자**가 포함되었는가? (N가지, top N)")
    w("- [ ] **호기심을 자극**하는가? (질문, 반전, 충격)")
    w("- [ ] **25자 이내**의 임팩트형을 고려했는가?")
    w("- [ ] Hook만 읽고도 더 읽고 싶어지는가?")
    w()

    w("### Body 작성 체크리스트")
    w()
    w("- [ ] **번호 리스트** 또는 **화살표 흐름**으로 구조화했는가?")
    w("- [ ] 한 줄이 50자를 넘지 않는가? (모바일 가독성)")
    w("- [ ] **북마크 가치**가 있는 실용적 정보인가?")
    w("- [ ] 이미지/미디어를 첨부했는가?")
    w("- [ ] 280자 제한 내에서 최대한 밀도 있게 썼는가?")
    w()

    w("### 피해야 할 패턴")
    w()
    w("- URL만 던지는 단순 링크 공유")
    w("- 맥락 없는 초단문 (텍스트 10자 미만)")
    w("- Hook 없이 바로 본론으로 들어가는 트윗")
    w("- @멘션으로 시작하는 대화형 트윗 (노출 제한)")
    w("- 패턴이 분류 불가능한 모호한 첫 줄")
    w()

    # ── 7. 부록 ──
    w("## 7. 부록")
    w()

    # 7.1 계정별 분석
    w("### 7.1 계정별 고성과 오리지널 트윗 분석")
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
    w("### 7.2 RT(리트윗) 분석")
    w()
    w(f"- 고성과 RT: **{high_rt_count:,}건** (고성과의 {high_rt_count/high_count*100:.1f}%)")
    w(f"- RT 평균 조회수: **{sum(i['views'] for i in high_rt_analyzed)//max(len(high_rt_analyzed),1):,}**")
    w(f"- 오리지널 평균 조회수: **{sum(i['views'] for i in high_orig_analyzed)//max(len(high_orig_analyzed),1):,}**")
    w(f"- RT가 고성과의 과반({high_rt_count/high_count*100:.0f}%)을 차지하지만, 이는 원작자의 콘텐츠 품질에 의한 것")
    w(f"- **자체 콘텐츠 역량 강화가 진정한 성장 전략**")
    w()

    # 7.3 방법론
    w("### 7.3 분석 방법론")
    w()
    w("1. **데이터 수집**: 56개 계정의 JSONL 파일에서 트윗 로드")
    w(f"2. **전처리**: viewCount 누락 제외, (fullText, source) 기준 중복 제거")
    w(f"3. **분류 기준**: 조회수 {THRESHOLD:,} 이상을 고성과로 분류")
    w("4. **RT 분리**: `fullText.startswith('RT @')` 기준으로 RT와 오리지널 분리")
    w("5. **Hook 분류**: 첫 번째 비어있지 않은 줄을 Hook으로 추출, 정규식 기반 다중 레이블 분류 (10개 유형)")
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
        "short_cryptic": [
            "[단어/문구]. (+ 이미지)",
            "[감탄사]. [한 문장].",
            "[주제] (미디어 첨부 필수)",
        ],
    }
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
    )

    with open(OUTPUT_MD, "w", encoding="utf-8") as f:
        f.write(md_content)

    print(f"  저장 완료: {OUTPUT_MD.name} ({OUTPUT_MD.stat().st_size / 1024:.0f} KB)")
    print(f"\n{'=' * 60}")
    print("역설계 완료!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
