"""Research Scout Agent (v1): profile -> arXiv search -> per-paper LLM judgment -> Top-K submissions.

Boundary: this module ends at ``submissions/<date>.jsonl``.  Everything after
(fetch / MinerU / normalize / reading / publish) stays in the fixed workflow.

Usage (from repo root):
    python -m research_pulse.scout --dry-run          # search + filter only, no LLM, no writes
    python -m research_pulse.scout                    # real run: DeepSeek judgments + submissions
    python -m research_pulse.scout --top-k 3 --max-judge 8

State (all append-only jsonl under data/scout/):
    judgments.jsonl    source_id -> verdict cache; judged papers never re-billed
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

ARXIV_API_URL = "http://export.arxiv.org/api/query"
DEEPSEEK_CHAT_COMPLETIONS_URL = "https://api.deepseek.com/chat/completions"  # mirrors production/adapters.py
DEFAULT_MODEL = "deepseek-v4-flash"

SCORE_RELEVANCE = {"high": 2.0, "mid": 1.0, "low": 0.0}
SCORE_NOVELTY = {"incremental": 0.8, "new_angle": 0.4, "unclear": 0.0}
VALID_RELEVANCE = set(SCORE_RELEVANCE)
VALID_NOVELTY = set(SCORE_NOVELTY)

ATOM = "{http://www.w3.org/2005/Atom}"


@dataclass
class PaperCandidate:
    source: str = "arxiv"
    source_id: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    published_at: str = ""


# ---------------------------------------------------------------- profile --

def load_profile(path: Path) -> dict[str, Any]:
    """Load scout profile (interests with keywords, dislikes)."""
    import yaml  # PyYAML is available in this environment

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    interests = []
    for item in data.get("interests") or []:
        if isinstance(item, dict) and item.get("topic"):
            interests.append(
                {
                    "topic": str(item["topic"]),
                    "keywords": [str(k) for k in item.get("keywords") or []],
                    "note": str(item.get("note", "")),
                }
            )
    return {"interests": interests, "dislikes": [str(d) for d in data.get("dislikes") or []]}


def read_source_ids(vault_root: Path) -> set[str]:
    """Past reads come from the vault itself (single source of truth), not a copy."""
    ids: set[str] = set()
    for sub in ("papers", "staging"):
        d = vault_root / sub
        if d.is_dir():
            ids.update(p.name for p in d.iterdir() if p.is_dir())
    return ids


def load_judgment_cache(path: Path) -> dict[str, dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                cache[record["source_id"]] = record
    return cache


# ------------------------------------------------------------------ arXiv --

def arxiv_rss_fetch(category: str) -> list[PaperCandidate]:
    """Fetch today's announced papers for one arXiv category via RSS."""
    url = f"https://rss.arxiv.org/rss/{category}"
    request = Request(url, headers={"User-Agent": "research-pulse-scout/0.1"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=90) as response:
                root = ET.fromstring(response.read())
            break
        except TimeoutError:
            if attempt == 2:
                raise
            time.sleep(5)
    out: list[PaperCandidate] = []
    for entry in root.iter("item"):
        abs_url = (entry.findtext("link") or "").strip()
        match = re.search(r"arxiv\.org/abs/(.+)$", abs_url)
        if not match:  # cross-list announcements etc.
            continue
        title = re.sub(r"\s+", " ", entry.findtext("title") or "").strip()
        if title.lower().startswith(("new submission", "replacement", "cross")):
            continue
        raw_date = entry.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
        out.append(
            PaperCandidate(
                source_id=match.group(1),
                title=title,
                authors=[
                    a.strip()
                    for a in re.split(r",\s*", entry.findtext("{http://purl.org/dc/elements/1.1/}creator") or "")
                    if a.strip()
                ],
                abstract=re.sub(r"\s+", " ", entry.findtext("description") or "").strip(),
                published_at=(raw_date if raw_date else datetime.now(timezone.utc).strftime("%Y-%m-%d")) + "T00:00:00Z",
            )
        )
    return out


def keyword_recall(candidate: PaperCandidate, interests: list[dict[str, Any]]) -> str | None:
    """Deterministic recall gate: at least one interest keyword in title+abstract."""
    haystack = f"{candidate.title}\n{candidate.abstract}".lower()
    for interest in interests:
        for keyword in interest["keywords"]:
            if keyword.lower() in haystack:
                return keyword
    return None


def dislike_hit(candidate: PaperCandidate, dislikes: list[str]) -> str | None:
    haystack = f"{candidate.title}\n{candidate.abstract}".lower()
    for term in dislikes:
        if term.lower() in haystack:
            return term
    return None


# --------------------------------------------------------------- DeepSeek --

def deepseek_judge(model: str, api_key: str, profile_digest: str, read_titles: list[str],
                   candidate: PaperCandidate, *, timeout: float = 60.0, max_retries: int = 2) -> dict[str, Any]:
    system = (
        "你是论文筛选评审。依据用户研究画像与已读论文列表，判断这篇新论文是否值得精读。只返回 JSON：\n"
        '{"relevance": "high|mid|low", "novelty": "incremental|new_angle|unclear", "reason": "<一句中文理由>"}\n'
        '标准：relevance=与画像主题的契合度；novelty=相对已读工作是否有机制/结论上的增量'
        '（incremental=明确增量, new_angle=新角度但增量不明, unclear=看不出）；reason 必须具体到机制或差异点。'
    )
    user = (
        f"# 用户研究画像\n{profile_digest}\n\n"
        f"# 过去已读（判断增量时对照）\n" + ("\n".join(read_titles[:20]) or "(无)") + "\n\n"
        f"# 候选论文\n标题: {candidate.title}\n作者: {', '.join(candidate.authors[:6])}\n"
        f"发布: {candidate.published_at}\n摘要: {candidate.abstract}"
    )
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "temperature": 0,
        "max_tokens": 400,
        "stream": False,
    }
    attempts = 0
    while True:
        try:
            request = Request(
                DEEPSEEK_CHAT_COMPLETIONS_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            break
        except (TimeoutError, URLError):
            attempts += 1
            if attempts > max_retries:
                raise
            time.sleep(2 * attempts)
    content = body["choices"][0]["message"]["content"]
    verdict = json.loads(re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip())
    if verdict.get("relevance") not in VALID_RELEVANCE or verdict.get("novelty") not in VALID_NOVELTY:
        raise RuntimeError(f"invalid judgment fields: {verdict}")
    verdict["reason"] = str(verdict.get("reason", "")).strip()
    return verdict


def scout_score(judgment: dict[str, Any]) -> float:
    return SCORE_RELEVANCE[judgment["relevance"]] + SCORE_NOVELTY[judgment["novelty"]]


# ------------------------------------------------------------------- main --

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scout agent: search arXiv, judge, submit Top-K.")
    parser.add_argument("--profile", type=Path, default=Path("scout/profile.yaml"))
    parser.add_argument("--vault", type=Path, default=Path("knowledge"))
    parser.add_argument("--state-dir", type=Path, default=Path("data/scout"))
    parser.add_argument("--submissions-dir", type=Path, default=Path("scout/submissions"))
    parser.add_argument("--categories", default="cs.LG,cs.IR,cs.AI", help="arXiv categories for RSS fetch")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--max-judge", type=int, default=8, help="LLM budget for one run")
    parser.add_argument("--model", default=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL))
    parser.add_argument("--dry-run", action="store_true", help="search + filters only; no DeepSeek, no writes")
    args = parser.parse_args(argv)

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key and (env := Path(".env")).exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("DEEPSEEK_API_KEY="):
                api_key = line.split("=", 1)[1].strip()
                break
    if args.dry_run:
        api_key = None

    profile = load_profile(args.profile)
    read_ids = read_source_ids(args.vault)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    cache_path = args.state_dir / "judgments.jsonl"
    cache = load_judgment_cache(cache_path)

    # -- search: RSS by category, keyword recall gate ------------------------
    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    candidates: dict[str, PaperCandidate] = {}
    for category in categories:
        entries = arxiv_rss_fetch(category)
        print(f"[rss] {category}: {len(entries)} new submissions")
        for c in entries:
            candidates.setdefault(c.source_id, c)
        time.sleep(3)  # arXiv courtesy rate limit
    print(f"[search] total unique: {len(candidates)}")

    # -- deterministic filters: recall gate, already-read, dislikes ----------
    rejected: list[tuple[str, str]] = []
    pool: list[PaperCandidate] = []
    for c in candidates.values():
        if c.source_id in read_ids:
            rejected.append((c.source_id, "already-read"))
            continue
        if term := dislike_hit(c, profile["dislikes"]):
            rejected.append((c.source_id, f"dislike:{term}"))
            continue
        if keyword_recall(c, profile["interests"]) is None:
            rejected.append((c.source_id, "no-keyword-hit"))
            continue
        pool.append(c)
    print(f"[filter] pool={len(pool)} rejected={len(rejected)} (read/dislike)")

    if args.dry_run:
        for c in pool:
            print(f"  - [{c.published_at[:10]}] {c.source_id}  {c.title[:80]}")
        print("[dry-run] stop before judging; nothing written.")
        return 0

    if not api_key:
        print("ERROR: DEEPSEEK_API_KEY not set (repo .env or environment).", file=sys.stderr)
        return 2

    # -- per-paper independent judgment (cached) ----------------------------
    profile_digest = "\n".join(
        f"- {i['topic']}: {'; '.join(i['keywords'])}" for i in profile["interests"]
    )
    read_titles = [f"- {p.name}" for p in sorted((args.vault / "papers").iterdir())] if (args.vault / "papers").is_dir() else []
    judged_new = 0
    for c in pool:
        if c.source_id in cache:
            continue
        if judged_new >= args.max_judge:
            break
        verdict = deepseek_judge(args.model, api_key, profile_digest, read_titles, c)
        record = {
            "source_id": c.source_id,
            "title": c.title,
            "judged_at": datetime.now(timezone.utc).isoformat(),
            "model": args.model,
            **verdict,
            "score": round(scout_score(verdict), 2),
        }
        cache[c.source_id] = record
        with cache_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        judged_new += 1
        print(f"[judge] {c.source_id} -> {verdict['relevance']}/{verdict['novelty']} ({record['score']}): {verdict['reason'][:60]}")
    print(f"[judge] new={judged_new} cached={len(cache) - judged_new}")

    # -- deterministic ranking + Top-K submission ---------------------------
    ranked = sorted(
        (cache[c.source_id] for c in pool if c.source_id in cache),
        key=lambda r: (-r["score"], r.get("judged_at", "")),
    )
    top = ranked[: args.top_k]
    args.submissions_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    submissions_path = args.submissions_dir / f"{today}.jsonl"
    existing = {
        json.loads(l)["source_id"]
        for l in submissions_path.read_text(encoding="utf-8").splitlines()
        if l.strip()
    } if submissions_path.exists() else set()
    written = 0
    with submissions_path.open("a", encoding="utf-8") as fh:
        for rank, record in enumerate(top, start=1):
            if record["source_id"] in existing:
                continue
            c = candidates[record["source_id"]]
            row = {
                "source": c.source,
                "source_id": c.source_id,
                "title": c.title,
                "authors": c.authors,
                "abstract": c.abstract,
                "published_at": c.published_at,
                "priority": rank,
                "scout_score": record["score"],
                "rationale": record["reason"],
                "relevance": record["relevance"],
                "novelty": record["novelty"],
                "judged_at": record["judged_at"],
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            }
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
    print(f"[submit] {written} -> {submissions_path} (top-k={args.top_k})")
    for record in top:
        print(f"  #{ranked.index(record)+1} {record['source_id']} score={record['score']} {record['title'][:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
