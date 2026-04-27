"""Collect current news with OpenAI web search and save selected items to Notion.

This script is designed for GitHub Actions.
It keeps API usage low by making one OpenAI call per run in the normal case.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from importlib import import_module
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests


def load_local_dotenv() -> None:
    try:
        dotenv = import_module("dotenv")
    except ModuleNotFoundError:
        return

    load_dotenv = getattr(dotenv, "load_dotenv", None)
    if callable(load_dotenv):
        load_dotenv()


load_local_dotenv()

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Environment
# -----------------------------------------------------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_SEARCH_CONTEXT_SIZE = os.getenv("OPENAI_SEARCH_CONTEXT_SIZE", "low")
OPENAI_WEB_SEARCH_TOOL = os.getenv("OPENAI_WEB_SEARCH_TOOL", "web_search_preview")
OPENAI_MAX_OUTPUT_TOKENS = int(os.getenv("OPENAI_MAX_OUTPUT_TOKENS", "3500"))

ARTICLES_PER_RUN = int(os.getenv("ARTICLES_PER_RUN", "3"))
LOOKBACK_LIMIT = int(os.getenv("LOOKBACK_LIMIT", "12"))
CANDIDATE_COUNT = int(os.getenv("CANDIDATE_COUNT", "9"))
RECENT_DAYS = int(os.getenv("RECENT_DAYS", "31"))
TIMEZONE = os.getenv("TIMEZONE", os.getenv("TZ", "Asia/Tokyo"))
NOTION_VERSION = os.getenv("NOTION_VERSION", "2022-06-28")

# Notion property names. Change these in GitHub Variables/Secrets if your DB differs.
PROP_CHECKPOINT = os.getenv("NOTION_PROP_CHECKPOINT", "チェックポイント")
PROP_TITLE = os.getenv("NOTION_PROP_TITLE", "タイトル")
PROP_DATE = os.getenv("NOTION_PROP_DATE", "日付")
PROP_IMPORTANT_POINTS = os.getenv("NOTION_PROP_IMPORTANT_POINTS", "重要ポイント")
PROP_CATEGORY = os.getenv("NOTION_PROP_CATEGORY", "カテゴリ")
PROP_URL = os.getenv("NOTION_PROP_URL", "URL")
PROP_RELIABILITY = os.getenv("NOTION_PROP_RELIABILITY", "信頼度")

CATEGORY_PRIORITY = ["外交", "世界情勢", "国内政治", "政策", "マクロ経済", "テクノロジー"]
RELIABILITY_VALUES = ["公式発表", "報道", "要確認"]

STOPWORDS_JA = {
    "する",
    "した",
    "して",
    "される",
    "された",
    "について",
    "による",
    "ため",
    "こと",
    "これ",
    "それ",
    "日本",
    "政府",
    "発表",
    "報道",
    "ニュース",
}


@dataclass
class ExistingArticle:
    title: str
    url: str
    category: str
    topic_key: str


@dataclass
class CandidateArticle:
    title: str
    date: str
    important_points: list[str]
    category: str
    url: str
    reliability: str
    source_name: str = ""
    topic_key: str = ""
    why_important: str = ""


# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------
def require_env() -> None:
    missing = [
        name
        for name, value in {
            "OPENAI_API_KEY": OPENAI_API_KEY,
            "NOTION_API_KEY": NOTION_API_KEY,
            "NOTION_DATABASE_ID": NOTION_DATABASE_ID,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


def now_jst() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


def normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
        # Drop query and fragment so UTM parameters do not bypass duplicate checks.
        normalized = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
        return normalized
    except Exception:
        return url.strip().lower()


def normalize_text(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[\s\u3000]+", " ", text)
    text = re.sub(r"[\[\]【】「」『』（）()!！?？,、。:：;；\-_/|｜]", " ", text)
    return text.strip()


def keyword_set(text: str) -> set[str]:
    text = normalize_text(text)
    words = re.findall(r"[a-z0-9]{3,}|[一-龥ぁ-んァ-ンー]{2,}", text)
    return {w for w in words if w not in STOPWORDS_JA}


def text_similarity(a: str, b: str) -> float:
    a_norm = normalize_text(a)
    b_norm = normalize_text(b)
    if not a_norm or not b_norm:
        return 0.0
    seq_score = SequenceMatcher(None, a_norm, b_norm).ratio()
    a_kw = keyword_set(a_norm)
    b_kw = keyword_set(b_norm)
    jaccard = len(a_kw & b_kw) / len(a_kw | b_kw) if (a_kw or b_kw) else 0.0
    return max(seq_score, jaccard)


def request_with_retry(method: str, url: str, *, headers: dict[str, str], json_body: dict[str, Any] | None = None, timeout: int = 60) -> dict[str, Any]:
    for attempt in range(1, 4):
        try:
            response = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 3:
                wait = 2**attempt
                logger.warning("Retryable HTTP %s from %s. Retrying in %ss.", response.status_code, url, wait)
                time.sleep(wait)
                continue
            if not response.ok:
                safe_body = response.text[:1200]
                raise RuntimeError(f"HTTP {response.status_code} from {url}: {safe_body}")
            return response.json()
        except requests.RequestException as exc:
            if attempt >= 3:
                raise RuntimeError(f"Request failed after retries: {url}: {exc}") from exc
            wait = 2**attempt
            logger.warning("Request error: %s. Retrying in %ss.", exc, wait)
            time.sleep(wait)
    raise RuntimeError("Unexpected retry state")


# -----------------------------------------------------------------------------
# Notion
# -----------------------------------------------------------------------------
def notion_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def extract_title(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    if prop.get("type") == "title":
        return "".join(part.get("plain_text", "") for part in prop.get("title", []))
    if "title" in prop:
        return "".join(part.get("plain_text", "") for part in prop.get("title", []))
    return ""


def extract_rich_text(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    if prop.get("type") == "rich_text":
        return "".join(part.get("plain_text", "") for part in prop.get("rich_text", []))
    if "rich_text" in prop:
        return "".join(part.get("plain_text", "") for part in prop.get("rich_text", []))
    return ""


def extract_select(prop: dict[str, Any] | None) -> str:
    """Return option names from either select or multi_select Notion properties."""
    if not prop:
        return ""

    select_value = prop.get("select")
    if isinstance(select_value, dict):
        return select_value.get("name", "")

    multi_select_value = prop.get("multi_select")
    if isinstance(multi_select_value, list):
        return ", ".join(
            item.get("name", "")
            for item in multi_select_value
            if isinstance(item, dict) and item.get("name")
        )

    return ""


def fetch_recent_articles(limit: int = LOOKBACK_LIMIT) -> list[ExistingArticle]:
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    body = {
        "page_size": limit,
        "sorts": [{"property": PROP_DATE, "direction": "descending"}],
    }
    try:
        data = request_with_retry("POST", url, headers=notion_headers(), json_body=body)
    except RuntimeError as exc:
        logger.warning("Date sort failed. Falling back to created_time sort. Reason: %s", exc)
        body = {
            "page_size": limit,
            "sorts": [{"timestamp": "created_time", "direction": "descending"}],
        }
        data = request_with_retry("POST", url, headers=notion_headers(), json_body=body)

    articles: list[ExistingArticle] = []
    for page in data.get("results", []):
        props = page.get("properties", {})
        title = extract_title(props.get(PROP_TITLE))
        url_value = ""
        url_prop = props.get(PROP_URL, {})
        if isinstance(url_prop, dict):
            url_value = url_prop.get("url") or ""
        category = extract_select(props.get(PROP_CATEGORY))
        points = extract_rich_text(props.get(PROP_IMPORTANT_POINTS))
        topic_key = normalize_text(f"{title} {category} {points}")[:220]
        articles.append(ExistingArticle(title=title, url=url_value, category=category, topic_key=topic_key))

    logger.info("Fetched %d recent Notion articles for duplicate checks.", len(articles))
    return articles


def build_notion_properties(article: CandidateArticle) -> dict[str, Any]:
    important_text = "\n".join(f"・{point}" for point in article.important_points[:3])
    return {
        PROP_TITLE: {"title": [{"text": {"content": article.title[:2000]}}]},
        PROP_DATE: {"date": {"start": article.date}},
        PROP_IMPORTANT_POINTS: {"rich_text": [{"text": {"content": important_text[:2000]}}]},
        PROP_CATEGORY: {"multi_select": [{"name": article.category}]},
        PROP_URL: {"url": article.url},
        PROP_RELIABILITY: {"multi_select": [{"name": article.reliability}]},
    }


def add_article_to_notion(article: CandidateArticle) -> str:
    url = "https://api.notion.com/v1/pages"
    body = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": build_notion_properties(article),
        "children": [
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": "なぜ重要か"}}]},
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": article.why_important[:2000] or "未記入"}}]},
            },
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": f"情報源: {article.source_name}"[:2000]}}]},
            },
        ],
    }
    data = request_with_retry("POST", url, headers=notion_headers(), json_body=body)
    page_id = data.get("id", "")
    logger.info("Added to Notion: %s (%s)", article.title, page_id)
    return page_id


# -----------------------------------------------------------------------------
# OpenAI
# -----------------------------------------------------------------------------
def openai_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }


def candidate_schema_hint() -> str:
    return json.dumps(
        {
            "articles": [
                {
                    "title": "記事内容を短く表す日本語タイトル",
                    "date": "YYYY-MM-DD",
                    "category": "外交 | 世界情勢 | 国内政治 | 政策 | マクロ経済 | テクノロジー",
                    "url": "https://...",
                    "reliability": "公式発表 | 報道 | 要確認",
                    "source_name": "媒体名または機関名",
                    "topic_key": "重複判定用に、固有名詞と論点を短くまとめた日本語キー",
                    "why_important": "なぜ社会理解に重要かを1〜2文で説明",
                    "important_points": ["重要点1", "重要点2", "重要点3"],
                }
            ]
        },
        ensure_ascii=False,
        indent=2,
    )


def build_research_prompt(existing: list[ExistingArticle], need_count: int, candidate_count: int) -> str:
    current_dt = now_jst()
    current_date = current_dt.strftime("%Y-%m-%d")
    cutoff_date = (current_dt.date() - timedelta(days=RECENT_DAYS)).isoformat()
    existing_payload = [
        {
            "title": item.title,
            "url": normalize_url(item.url),
            "category": item.category,
            "topic_key": item.topic_key,
        }
        for item in existing
    ]

    return f"""
あなたは日本語で時事ニュースを整理するリサーチ担当です。
今日の日付は日本時間で {current_date} です。
公開Web検索を使い、直近の重要ニュース候補を {candidate_count} 件集めてください。
最終的にシステム側で {need_count} 件だけNotionへ保存します。

目的:
- 毎日、重要な時事ニュースを自動収集する
- 単なるニュース一覧ではなく、「なぜ重要か」まで整理する

対象カテゴリと優先順位:
1. 外交
2. 世界情勢
3. 国内政治
4. 政策
5. マクロ経済
6. テクノロジー

選定基準:
- 日本社会・国際情勢への影響が大きい
- 政策、外交、安全保障、経済、技術動向に関係する
- 信頼できる情報源を優先する
- 同程度に重要ならカテゴリ優先順位を使う
- ただし同じカテゴリに偏りすぎず、可能なら複数カテゴリから選ぶ

重複除外:
以下は直近でNotionに保存済みです。同じURL、同一テーマ、同一ニュース、同じニュースを扱う別メディア記事は候補から除外してください。
{json.dumps(existing_payload, ensure_ascii=False, indent=2)}

信頼度:
- 政府機関、省庁、企業、国際機関などの一次情報に基づく場合: 公式発表
- 新聞社、通信社、専門メディアなどの報道記事に基づく場合: 報道
- 情報源が限定的、推測が含まれる、または確認が必要な場合: 要確認

出力ルール:
- 必ずJSONのみを返してください。Markdown、説明文、コードブロックは禁止です。
- URLは実際に参照できる記事または公式発表のURLにしてください。
- important_points は3点、各80字以内にしてください。
- title は記事見出しの丸写しではなく、内容がわかる簡潔な日本語題名にしてください。
- category と reliability は指定された値だけを使ってください。
- topic_key は重複判定用に、国名・組織名・制度名・論点を含めてください。

JSON形式:
{candidate_schema_hint()}
""".strip()


def extract_response_text(data: dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]

    chunks: list[str] = []
    for output_item in data.get("output", []) or []:
        if isinstance(output_item, dict):
            for content in output_item.get("content", []) or []:
                if not isinstance(content, dict):
                    continue
                if isinstance(content.get("text"), str):
                    chunks.append(content["text"])
                elif isinstance(content.get("content"), str):
                    chunks.append(content["content"])

    # Compatibility with Chat Completions-like responses.
    for choice in data.get("choices", []) or []:
        message = choice.get("message", {}) if isinstance(choice, dict) else {}
        if isinstance(message.get("content"), str):
            chunks.append(message["content"])

    return "\n".join(chunks).strip()


def parse_json_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Last resort: extract the outermost JSON object.
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def research_candidates(existing: list[ExistingArticle], need_count: int, candidate_count: int) -> list[CandidateArticle]:
    prompt = build_research_prompt(existing, need_count, candidate_count)
    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {
                "role": "system",
                "content": "あなたは信頼性の高い日本語の時事ニュース調査アシスタントです。必ずWeb検索を使い、JSONのみを返します。",
            },
            {"role": "user", "content": prompt},
        ],
        "tools": [
            {
                "type": OPENAI_WEB_SEARCH_TOOL,
                "search_context_size": OPENAI_SEARCH_CONTEXT_SIZE,
            }
        ],
        "max_output_tokens": OPENAI_MAX_OUTPUT_TOKENS,
    }

    logger.info("Calling OpenAI Responses API. model=%s candidate_count=%d", OPENAI_MODEL, candidate_count)
    data = request_with_retry("POST", "https://api.openai.com/v1/responses", headers=openai_headers(), json_body=payload, timeout=120)
    text = extract_response_text(data)
    if not text:
        raise RuntimeError("OpenAI response did not contain output text.")

    parsed = parse_json_response(text)
    raw_articles = parsed.get("articles", [])
    if not isinstance(raw_articles, list):
        raise RuntimeError("OpenAI JSON response does not contain an articles list.")

    candidates: list[CandidateArticle] = []
    for raw in raw_articles:
        if not isinstance(raw, dict):
            continue
        important_points = raw.get("important_points") or []
        if not isinstance(important_points, list):
            important_points = [str(important_points)]
        article = CandidateArticle(
            title=str(raw.get("title", "")).strip(),
            date=str(raw.get("date", now_jst().strftime("%Y-%m-%d"))).strip(),
            important_points=[str(p).strip() for p in important_points if str(p).strip()][:3],
            category=str(raw.get("category", "")).strip(),
            url=str(raw.get("url", "")).strip(),
            reliability=str(raw.get("reliability", "報道")).strip(),
            source_name=str(raw.get("source_name", "")).strip(),
            topic_key=str(raw.get("topic_key", "")).strip(),
            why_important=str(raw.get("why_important", "")).strip(),
        )
        candidates.append(article)

    logger.info("OpenAI returned %d candidate articles.", len(candidates))
    return candidates


# -----------------------------------------------------------------------------
# Selection and duplicate checks
# -----------------------------------------------------------------------------
def is_valid_candidate(article: CandidateArticle) -> tuple[bool, str]:
    if not article.title:
        return False, "missing title"
    if not article.url.startswith("http"):
        return False, "missing or invalid URL"
    if article.category not in CATEGORY_PRIORITY:
        return False, f"invalid category: {article.category}"
    if article.reliability not in RELIABILITY_VALUES:
        return False, f"invalid reliability: {article.reliability}"
    if len(article.important_points) < 3:
        return False, "important_points must contain 3 items"
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", article.date):
        return False, f"invalid date format: {article.date}"

    try:
        article_date = datetime.strptime(article.date, "%Y-%m-%d").date()
    except ValueError:
        return False, f"invalid date value: {article.date}"

    today = now_jst().date()
    cutoff = today - timedelta(days=RECENT_DAYS)

    if article_date < cutoff:
        return False, f"article date is older than {RECENT_DAYS} days: {article.date}"
    if article_date > today:
        return False, f"article date is in the future: {article.date}"

    return True, "ok"


def is_duplicate(article: CandidateArticle, existing: list[ExistingArticle], selected: list[CandidateArticle]) -> tuple[bool, str]:
    article_url = normalize_url(article.url)
    article_topic = normalize_text(f"{article.topic_key} {article.title} {' '.join(article.important_points)}")

    for old in existing:
        if article_url and article_url == normalize_url(old.url):
            return True, f"same URL as existing: {old.title}"
        old_topic = normalize_text(f"{old.topic_key} {old.title}")
        if text_similarity(article_topic, old_topic) >= 0.58:
            return True, f"similar topic to existing: {old.title}"

    for chosen in selected:
        if article_url and article_url == normalize_url(chosen.url):
            return True, f"same URL as selected: {chosen.title}"
        chosen_topic = normalize_text(f"{chosen.topic_key} {chosen.title} {' '.join(chosen.important_points)}")
        if text_similarity(article_topic, chosen_topic) >= 0.62:
            return True, f"similar topic to selected: {chosen.title}"

    return False, "not duplicate"


def category_rank(article: CandidateArticle) -> int:
    try:
        return CATEGORY_PRIORITY.index(article.category)
    except ValueError:
        return len(CATEGORY_PRIORITY)


def select_articles(candidates: list[CandidateArticle], existing: list[ExistingArticle], need_count: int) -> list[CandidateArticle]:
    valid: list[CandidateArticle] = []
    for article in candidates:
        ok, reason = is_valid_candidate(article)
        if not ok:
            logger.warning("Skipping invalid candidate: %s / %s", reason, article.title)
            continue
        valid.append(article)

    valid.sort(key=lambda a: (category_rank(a), a.title))

    selected: list[CandidateArticle] = []
    used_categories: set[str] = set()

    # First pass: prefer category diversity.
    for article in valid:
        if len(selected) >= need_count:
            break
        if article.category in used_categories and len(used_categories) < min(need_count, len(CATEGORY_PRIORITY)):
            continue
        dup, reason = is_duplicate(article, existing, selected)
        if dup:
            logger.info("Duplicate skipped: %s / %s", reason, article.title)
            continue
        selected.append(article)
        used_categories.add(article.category)

    # Second pass: fill remaining slots by priority regardless of category diversity.
    for article in valid:
        if len(selected) >= need_count:
            break
        if any(normalize_url(article.url) == normalize_url(chosen.url) for chosen in selected):
            continue
        dup, reason = is_duplicate(article, existing, selected)
        if dup:
            logger.info("Duplicate skipped: %s / %s", reason, article.title)
            continue
        selected.append(article)
        used_categories.add(article.category)

    logger.info("Selected %d/%d articles.", len(selected), need_count)
    return selected


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main() -> int:
    require_env()
    logger.info("Starting current news research. timezone=%s articles_per_run=%d lookback=%d", TIMEZONE, ARTICLES_PER_RUN, LOOKBACK_LIMIT)

    existing = fetch_recent_articles(LOOKBACK_LIMIT)
    candidates = research_candidates(existing, ARTICLES_PER_RUN, CANDIDATE_COUNT)
    selected = select_articles(candidates, existing, ARTICLES_PER_RUN)

    # Fallback: make one additional call only when needed.
    if len(selected) < ARTICLES_PER_RUN:
        shortfall = ARTICLES_PER_RUN - len(selected)
        logger.warning("Only %d articles selected. Running one fallback research call for %d more.", len(selected), shortfall)
        expanded_existing = existing + [
            ExistingArticle(title=a.title, url=a.url, category=a.category, topic_key=normalize_text(f"{a.topic_key} {a.title}"))
            for a in selected
        ]
        fallback_candidates = research_candidates(expanded_existing, shortfall, max(CANDIDATE_COUNT, shortfall * 4))
        selected = selected + select_articles(fallback_candidates, expanded_existing, shortfall)

    if not selected:
        logger.warning("No new articles were added. This is not treated as a failure because all candidates may have been duplicates.")
        return 0

    added = 0
    for article in selected[:ARTICLES_PER_RUN]:
        try:
            add_article_to_notion(article)
            added += 1
        except Exception as exc:
            logger.exception("Failed to add article to Notion: %s / %s", article.title, exc)

    logger.info("Finished. Added %d article(s) to Notion.", added)
    if added == 0:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        raise SystemExit(1)
