"""
X 트윗 데이터 분석 스크립트
- x 자료/ 폴더의 JSONL 파일에서 트윗 데이터를 읽어 분석
- viewCount >= 100,000 고성과 트윗 필터링
- 통계 및 결과를 high_performing_tweets.json에 저장
"""

import json
import os
import zipfile
from pathlib import Path
from collections import defaultdict

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "x 자료"
OUTPUT_FILE = BASE_DIR / "high_performing_tweets.json"
THRESHOLD = 100_000


def load_all_tweets():
    """모든 JSONL 파일에서 트윗을 로드하고 source(계정명) 필드를 추가한다."""
    tweets = []
    skipped = 0

    for filepath in sorted(DATA_DIR.glob("*.jsonl")):
        account_name = filepath.stem  # 파일명 = 계정명
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                tweet = json.loads(line)
                tweet["source"] = account_name

                if "viewCount" not in tweet or tweet["viewCount"] is None:
                    skipped += 1
                    continue

                tweets.append(tweet)

    return tweets, skipped


def inspect_zip():
    """zip 파일 내용을 확인하여 구조를 출력한다."""
    zip_files = list(DATA_DIR.glob("*.zip"))
    if not zip_files:
        print("  zip 파일 없음")
        return

    for zf_path in zip_files:
        print(f"  {zf_path.name}:")
        with zipfile.ZipFile(zf_path, "r") as zf:
            names = zf.namelist()
            # 최상위 디렉토리/파일만 표시
            top_level = set()
            for name in names:
                parts = name.split("/")
                if len(parts) > 1:
                    top_level.add(parts[0] + "/" + parts[1])
                else:
                    top_level.add(parts[0])
            for item in sorted(top_level)[:15]:
                print(f"    {item}")
            if len(top_level) > 15:
                print(f"    ... 외 {len(top_level) - 15}개")
            print(f"    (총 {len(names)}개 파일)")


def calc_engagement_rate(tweet):
    """engagement rate = (likes + quotes + replies + retweets + bookmarks) / views"""
    views = tweet.get("viewCount", 0)
    if not views:
        return 0.0
    engagement = (
        tweet.get("likeCount", 0)
        + tweet.get("quoteCount", 0)
        + tweet.get("replyCount", 0)
        + tweet.get("retweetCount", 0)
        + tweet.get("bookmarkCount", 0)
    )
    return engagement / views


def compute_group_stats(tweets):
    """트윗 그룹의 평균 지표를 계산한다."""
    if not tweets:
        return {
            "count": 0,
            "avg_viewCount": 0,
            "avg_likeCount": 0,
            "avg_quoteCount": 0,
            "avg_replyCount": 0,
            "avg_retweetCount": 0,
            "avg_bookmarkCount": 0,
            "avg_engagement_rate": 0,
        }

    n = len(tweets)
    return {
        "count": n,
        "avg_viewCount": round(sum(t.get("viewCount", 0) for t in tweets) / n, 1),
        "avg_likeCount": round(sum(t.get("likeCount", 0) for t in tweets) / n, 1),
        "avg_quoteCount": round(sum(t.get("quoteCount", 0) for t in tweets) / n, 1),
        "avg_replyCount": round(sum(t.get("replyCount", 0) for t in tweets) / n, 1),
        "avg_retweetCount": round(sum(t.get("retweetCount", 0) for t in tweets) / n, 1),
        "avg_bookmarkCount": round(sum(t.get("bookmarkCount", 0) for t in tweets) / n, 1),
        "avg_engagement_rate": round(sum(calc_engagement_rate(t) for t in tweets) / n * 100, 3),
    }


def main():
    print("=" * 60)
    print("X 트윗 데이터 분석")
    print("=" * 60)

    # zip 파일 구조 확인
    print("\n[참고] zip 파일 구조:")
    inspect_zip()

    # 1단계: 데이터 로드
    print("\n[1단계] 데이터 로드 중...")
    tweets, skipped = load_all_tweets()
    print(f"  총 로드된 트윗: {len(tweets):,}건")
    print(f"  viewCount 누락으로 제외: {skipped}건")

    # 2단계: 필터링
    print(f"\n[2단계] 고성과 트윗 필터링 (viewCount >= {THRESHOLD:,})...")
    high = [t for t in tweets if t.get("viewCount", 0) >= THRESHOLD]
    low = [t for t in tweets if t.get("viewCount", 0) < THRESHOLD]
    print(f"  고성과 (high_performing): {len(high):,}건")
    print(f"  저성과 (low_performing):  {len(low):,}건")
    print(f"  고성과 비율: {len(high) / len(tweets) * 100:.1f}%")

    # 3단계: 통계 계산
    print("\n[3단계] 통계 분석...")
    high_stats = compute_group_stats(high)
    low_stats = compute_group_stats(low)

    print(f"\n  {'지표':<25} {'고성과':>15} {'저성과':>15}")
    print(f"  {'-' * 55}")
    for key in ["avg_viewCount", "avg_likeCount", "avg_quoteCount",
                 "avg_replyCount", "avg_retweetCount", "avg_bookmarkCount"]:
        label = key.replace("avg_", "평균 ")
        print(f"  {label:<25} {high_stats[key]:>15,.1f} {low_stats[key]:>15,.1f}")
    print(f"  {'평균 engagement rate(%)':<25} {high_stats['avg_engagement_rate']:>15.3f} {low_stats['avg_engagement_rate']:>15.3f}")

    # 계정별 통계
    print("\n  [계정별 고성과 트윗 분석]")
    account_all = defaultdict(int)
    account_high = defaultdict(int)
    for t in tweets:
        account_all[t["source"]] += 1
    for t in high:
        account_high[t["source"]] += 1

    account_stats = {}
    for acc in sorted(account_all.keys()):
        total = account_all[acc]
        high_count = account_high.get(acc, 0)
        ratio = high_count / total * 100 if total else 0
        account_stats[acc] = {
            "total_tweets": total,
            "high_performing_count": high_count,
            "high_performing_ratio_pct": round(ratio, 1),
        }

    # 고성과 트윗이 있는 계정만 출력 (상위 20개)
    ranked = sorted(account_stats.items(), key=lambda x: x[1]["high_performing_count"], reverse=True)
    print(f"  {'계정':<20} {'전체':>8} {'고성과':>8} {'비율(%)':>8}")
    print(f"  {'-' * 46}")
    shown = 0
    for acc, stats in ranked:
        if stats["high_performing_count"] > 0:
            print(f"  {acc:<20} {stats['total_tweets']:>8} {stats['high_performing_count']:>8} {stats['high_performing_ratio_pct']:>8.1f}")
            shown += 1
            if shown >= 20:
                remaining = sum(1 for _, s in ranked[shown:] if s["high_performing_count"] > 0)
                if remaining > 0:
                    print(f"  ... 외 {remaining}개 계정")
                break

    # 4단계: JSON 저장
    print(f"\n[4단계] 결과 저장 → {OUTPUT_FILE.name}")
    high_sorted = sorted(high, key=lambda t: t.get("viewCount", 0), reverse=True)

    # engagement_rate 필드 추가
    for t in high_sorted:
        t["engagement_rate"] = round(calc_engagement_rate(t) * 100, 3)

    output = {
        "summary": {
            "total_tweets": len(tweets),
            "high_performing_count": len(high),
            "low_performing_count": len(low),
            "high_performing_ratio_pct": round(len(high) / len(tweets) * 100, 1),
            "viewCount_skipped": skipped,
            "threshold": THRESHOLD,
        },
        "comparison": {
            "high_performing": high_stats,
            "low_performing": low_stats,
        },
        "account_stats": account_stats,
        "high_performing_tweets": high_sorted,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"  저장 완료! ({OUTPUT_FILE.stat().st_size / 1024:.0f} KB)")
    print(f"\n{'=' * 60}")
    print("분석 완료!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
