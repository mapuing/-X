# Mapuing Tweet Generator — Work Instructions

## Data-Driven Work Principle (MANDATORY)

When adding or changing any features, rules, formulas, ingredients, or types, you MUST first investigate the raw data in the `x 자료/` folder. Never rely on guesses or assumptions.

### Investigation Scope

- `x 자료/*.jsonl` — Raw tweet data from 56 accounts (includes view counts, likes, engagement metrics)
- `x 자료/extracted/x-algorithm-main/` — X algorithm source code (recommendation/ranking logic reference)
- `high_performing_tweets.json` — Filtered high-performing tweets (100K+ views)
- `tweet_formulas.md`, `tweet_formulas_v2.md` — Formulas and patterns derived from prior analysis
- `llm_hook_cache.json` — Hook classification cache

### Procedure

```
1. Check actual patterns, frequency, and performance from JSONL data in x 자료/
2. Cross-reference with existing analysis (tweet_formulas*.md, high_performing_tweets.json)
3. Only add/change items that have confirmed data evidence
4. Run compatibility checks (see checklist below)
```

---

## Compatibility Checks (MANDATORY)

When modifying `tweet_generator.py`, `create_tweet.py`, or related data files, you MUST validate the checklist below. After changes, run `python tweet_generator.py` at least once to confirm 20 topics are generated without errors.

### 1. Internal Consistency (tweet_generator.py)

- **INGREDIENTS keys ↔ FORMULAS template variables**: Every `{key}` or `{key#particle}` used in FORMULAS templates must exist as a key in INGREDIENTS
- **TYPE_INFO.subtypes ↔ FORMULAS keys**: Every subtype listed in TYPE_INFO must have a corresponding key in FORMULAS
- **HYBRID_FORMULAS.parent_type/subtype**: Each entry's parent_type must be a TYPE_INFO key, and subtype must be in that parent_type's subtypes list
- **PROVEN_TOPICS structure**: `PROVEN_TOPICS[type_name][subtype_name]` — type_name must be a TYPE_INFO key, subtype_name must match a FORMULAS key
- **Particle syntax**: Particles in `{key#particle}` must only use values defined in `_PARTICLES` dict (이, 을, 은, 와, 이라면, 으로)
- **SLOT_DISTRIBUTION**: All 7 types must be evenly distributed
- **_CORE_INGREDIENTS**: The set of pool names for batch-level deduplication must match actual INGREDIENTS keys

### 2. Cross-File Compatibility

| What Changed | Affected File | What to Verify |
|---|---|---|
| INGREDIENTS key add/delete/rename | `create_tweet.py` | Keys match `topic_quality_cache.json` |
| INGREDIENTS key add/delete/rename | `topic_quality_cache.json` | Quality scores exist for new keys |
| TYPE_INFO type name change | `create_tweet.py` | `TYPE_TO_HOOKS`, `LIFT_TABLE` mappings are valid |
| TYPE_INFO type name change | `tweet_generator.py` | `TYPE_FRAMINGS`, `SLOT_DISTRIBUTION`, `PROVEN_TOPICS` are in sync |
| FORMULAS subtype add/delete | `create_tweet.py` | `suggest_combo()` type mapping is valid |
| Hook classification change | `reverse_engineer_tweets.py`, `reverse_engineer_v2.py` | HOOK_PATTERNS keys and HOOK_LABELS_KR are in sync |
| Hook classification change | `llm_hook_cache.json` | hook_type_overrides labels are valid |

### 3. Validation Execution Order

```
1. Make code changes
2. python tweet_generator.py  ← Confirm 20 topics generated without errors
3. Visually inspect cross-variable and particle rendering results
4. If create_tweet.py was affected → also run python create_tweet.py
```

## Project Structure Summary

- `tweet_generator.py` — Core. Generates 20 topics via ingredient + formula combination
- `create_tweet.py` — Imports tweet_generator.TopicRecommender, generates 80 candidates → selects top 20
- `analyze_tweets.py` → Produces `high_performing_tweets.json` (analysis pipeline)
- `reverse_engineer_tweets.py` / `reverse_engineer_v2.py` → Produces `tweet_formulas.md` / `tweet_formulas_v2.md`
- `build_llm_cache.py` → Produces `llm_hook_cache.json`
- `validate_analysis.py` → Produces `validation_report.md`
- `audit_pipeline.py` → Produces `audit_report.md`
- `customize_mapuing.py` — Imports functions from reverse_engineer_tweets
