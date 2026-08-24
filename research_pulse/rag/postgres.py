"""PostgreSQL FTS adapter for the first self-built ResearchRAG baseline."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import re
from typing import Any, Iterator, Sequence

from research_pulse.knowledge.models import (
    DurableEvidenceAnchor,
    KnowledgeAssetError,
    KnowledgeBundle,
    render_provenance,
)
from research_pulse.rag.chunking import chunk_bundle
from research_pulse.rag.contracts import EvidenceHit, IndexReceipt, SearchRequest


SCOPED_FALLBACK_MINIMUM_HITS = 2


SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS knowledge_assets (
    knowledge_id TEXT NOT NULL,
    knowledge_version TEXT NOT NULL,
    title TEXT NOT NULL,
    domain TEXT NOT NULL,
    publication_status TEXT NOT NULL,
    evidence_level TEXT NOT NULL,
    source_urls JSONB NOT NULL,
    content_sha256 TEXT NOT NULL,
    supersedes TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1,
    provenance_sha256 TEXT,
    is_current BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (knowledge_id, knowledge_version)
);

CREATE INDEX IF NOT EXISTS knowledge_assets_active_idx
    ON knowledge_assets (publication_status, is_current, domain);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id TEXT PRIMARY KEY,
    knowledge_id TEXT NOT NULL,
    knowledge_version TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    section TEXT NOT NULL,
    anchor_id TEXT NOT NULL,
    content TEXT NOT NULL,
    claim_id TEXT,
    claim_type TEXT,
    source_anchors JSONB NOT NULL DEFAULT '[]'::jsonb,
    search_vector TSVECTOR NOT NULL,
    FOREIGN KEY (knowledge_id, knowledge_version)
        REFERENCES knowledge_assets (knowledge_id, knowledge_version)
        ON DELETE CASCADE,
    UNIQUE (knowledge_id, knowledge_version, ordinal)
);

CREATE INDEX IF NOT EXISTS knowledge_chunks_fts_idx
    ON knowledge_chunks USING GIN (search_vector);

ALTER TABLE knowledge_assets ADD COLUMN IF NOT EXISTS schema_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE knowledge_assets ADD COLUMN IF NOT EXISTS provenance_sha256 TEXT;
ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS claim_id TEXT;
ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS claim_type TEXT;
ALTER TABLE knowledge_chunks ADD COLUMN IF NOT EXISTS source_anchors JSONB NOT NULL DEFAULT '[]'::jsonb;
"""


class PostgresResearchRAG:
    """Idempotent publish plus metadata-filtered multilingual FTS search.

    The first ResearchRAG baseline deliberately stays dependency-light and uses
    PostgreSQL FTS rather than a hosted embedding service.  Query expansion at
    this seam bridges common Chinese research questions to the English terms
    used by arXiv papers, while keeping the answer policy and evidence filters
    unchanged.
    """

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def initialize(self) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(SCHEMA_SQL)

    def publish(self, bundle: KnowledgeBundle) -> IndexReceipt:
        if not bundle.answer_eligible:
            raise KnowledgeAssetError("Legacy bundles cannot enter the fact index.")
        asset = bundle.asset
        if asset.publication_status != "published":
            raise KnowledgeAssetError("Only publication_status='published' assets may be indexed.")
        if asset.schema_version not in {2, 3} or not asset.provenance_sha256:
            raise KnowledgeAssetError("ResearchRAG requires a persisted schema-v2+ bundle.")
        expected_provenance_hash = sha256(render_provenance(bundle).encode("utf-8")).hexdigest()
        if asset.provenance_sha256 != expected_provenance_hash:
            raise KnowledgeAssetError("ResearchRAG rejected a bundle with a mismatched provenance hash.")
        chunks = chunk_bundle(bundle)
        if not chunks:
            raise KnowledgeAssetError("A published asset needs at least one non-empty chunk.")
        receipt = IndexReceipt(
            knowledge_id=asset.knowledge_id,
            knowledge_version=asset.knowledge_version,
            chunk_ids=tuple(chunk.chunk_id for chunk in chunks),
        )
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT content_sha256, provenance_sha256 FROM knowledge_assets
                    WHERE knowledge_id = %(knowledge_id)s AND knowledge_version = %(knowledge_version)s
                    """,
                    {"knowledge_id": asset.knowledge_id, "knowledge_version": asset.knowledge_version},
                )
                existing = cursor.fetchone()
                if existing:
                    existing_body_hash = existing[0] if not isinstance(existing, dict) else existing["content_sha256"]
                    existing_provenance_hash = existing[1] if not isinstance(existing, dict) else existing["provenance_sha256"]
                    if existing_body_hash != asset.content_sha256 or existing_provenance_hash != asset.provenance_sha256:
                        raise KnowledgeAssetError("A knowledge ID/version pair cannot be republished with new content.")
                    return receipt
                cursor.execute(
                    "UPDATE knowledge_assets SET is_current = FALSE WHERE knowledge_id = %(knowledge_id)s",
                    {"knowledge_id": asset.knowledge_id},
                )
                cursor.execute(
                    """
                    INSERT INTO knowledge_assets (
                        knowledge_id, knowledge_version, title, domain, publication_status,
                        evidence_level, source_urls, content_sha256, supersedes,
                        schema_version, provenance_sha256, is_current
                    ) VALUES (
                        %(knowledge_id)s, %(knowledge_version)s, %(title)s, %(domain)s,
                        %(publication_status)s, %(evidence_level)s, %(source_urls)s::jsonb,
                        %(content_sha256)s, %(supersedes)s, %(schema_version)s,
                        %(provenance_sha256)s, TRUE
                    )
                    """,
                    {
                        **asdict(asset),
                        "source_urls": _json_dumps(list(asset.source_urls)),
                    },
                )
                for chunk in chunks:
                    cursor.execute(
                        """
                        INSERT INTO knowledge_chunks (
                            chunk_id, knowledge_id, knowledge_version, ordinal, section,
                            anchor_id, content, claim_id, claim_type, source_anchors, search_vector
                        ) VALUES (
                            %(chunk_id)s, %(knowledge_id)s, %(knowledge_version)s, %(ordinal)s,
                            %(section)s, %(anchor_id)s, %(content)s, %(claim_id)s,
                            %(claim_type)s, %(source_anchors)s::jsonb,
                            to_tsvector('simple', %(content)s)
                        )
                        """,
                        {
                            **asdict(chunk),
                            "content": chunk.text,
                            "source_anchors": _json_dumps([asdict(anchor) for anchor in chunk.source_anchors]),
                        },
                    )
        return receipt

    def search(self, request: SearchRequest) -> Sequence[EvidenceHit]:
        query = " ".join(request.query.split())
        if len(query) < 2:
            return []
        search_query = expand_search_query(query)
        filters: list[str] = []
        parameters: dict[str, Any] = {
            "query": query,
            "search_query": search_query,
            "limit": max(1, min(request.limit, 50)),
        }
        if request.published_only:
            filters.extend(["a.publication_status = 'published'", "a.is_current = TRUE"])
        filters.extend(["a.schema_version IN (2, 3)", "c.claim_id IS NOT NULL", "c.claim_type = 'source_fact'"])
        if request.domain:
            filters.append("a.domain = %(domain)s")
            parameters["domain"] = request.domain
        if request.knowledge_ids:
            filters.append("a.knowledge_id = ANY(%(knowledge_ids)s)")
            parameters["knowledge_ids"] = list(request.knowledge_ids)
        if request.claim_types:
            filters.append("c.claim_type = ANY(%(claim_types)s)")
            parameters["claim_types"] = list(request.claim_types)
        ranked_where = " AND ".join(
            [*filters, "c.search_vector @@ websearch_to_tsquery('simple', %(search_query)s)"]
        )
        sql = f"""
            SELECT
                c.chunk_id, c.knowledge_id, c.knowledge_version, a.title, c.content,
                a.source_urls->>0 AS source_url, c.anchor_id, c.claim_id, c.claim_type,
                c.source_anchors,
                ts_rank_cd(c.search_vector, websearch_to_tsquery('simple', %(search_query)s)) AS keyword_score
            FROM knowledge_chunks c
            JOIN knowledge_assets a
              ON a.knowledge_id = c.knowledge_id AND a.knowledge_version = c.knowledge_version
            WHERE {ranked_where}
            ORDER BY keyword_score DESC, c.ordinal ASC
            LIMIT %(limit)s
        """
        with self._connection(row_factory=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, parameters)
                rows = cursor.fetchall()
        if request.knowledge_ids and len(rows) < min(SCOPED_FALLBACK_MINIMUM_HITS, parameters["limit"]):
            # FTS is lexical and can miss a Chinese question over an English paper.
            # For an explicitly selected paper, return only that paper's current,
            # source-addressable claims rather than treating the miss as a lack of knowledge.
            scoped_where = " AND ".join(filters)
            scoped_sql = f"""
                SELECT
                    c.chunk_id, c.knowledge_id, c.knowledge_version, a.title, c.content,
                    a.source_urls->>0 AS source_url, c.anchor_id, c.claim_id, c.claim_type,
                    c.source_anchors, 0.0 AS keyword_score
                FROM knowledge_chunks c
                JOIN knowledge_assets a
                  ON a.knowledge_id = c.knowledge_id AND a.knowledge_version = c.knowledge_version
                WHERE {scoped_where}
                ORDER BY c.ordinal ASC
                LIMIT %(limit)s
            """
            with self._connection(row_factory=True) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(scoped_sql, parameters)
                    rows = cursor.fetchall()
        return [
            EvidenceHit(
                chunk_id=row["chunk_id"],
                knowledge_id=row["knowledge_id"],
                knowledge_version=row["knowledge_version"],
                title=row["title"],
                text=row["content"],
                source_url=row["source_url"],
                anchor_id=row["anchor_id"],
                dense_score=None,
                keyword_score=float(row["keyword_score"]),
                fused_score=float(row["keyword_score"]),
                claim_id=row["claim_id"],
                claim_type=row["claim_type"],
                source_anchors=tuple(_durable_anchor(value) for value in row["source_anchors"]),
            )
            for row in rows
        ]

    @contextmanager
    def _connection(self, *, row_factory: bool = False) -> Iterator[Any]:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as error:
            raise RuntimeError(
                "psycopg is not installed. Run: .\\.venv\\Scripts\\python -m pip install -r requirements-rag.txt"
            ) from error
        kwargs = {"row_factory": dict_row} if row_factory else {}
        with psycopg.connect(self.database_url, **kwargs) as connection:
            yield connection


_SEARCH_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "be",
        "can",
        "does",
        "for",
        "how",
        "is",
        "of",
        "the",
        "this",
        "these",
        "to",
        "what",
        "which",
        "with",
    }
)

# The aliases are intentionally small and high precision.  They are not a
# machine translation system; they cover the recurring question vocabulary of
# a personal paper library without sending every query to an LLM.
_CROSS_LANGUAGE_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("论文", ("paper", "article")),
    ("文章", ("paper", "article")),
    ("研究", ("research", "study")),
    ("方法", ("method", "methods", "approach", "approaches", "mechanism", "framework")),
    ("机制", ("mechanism", "mechanisms", "method", "approach")),
    ("框架", ("framework", "frameworks", "method", "architecture")),
    ("问题", ("problem", "challenge", "task")),
    ("目标", ("objective", "goal", "task")),
    ("实验", ("experiment", "experiments", "evaluation", "evaluate", "evaluated", "benchmark")),
    ("结果", ("result", "results", "finding", "findings", "performance")),
    ("性能", ("performance", "accuracy", "metric")),
    ("指标", ("metric", "score", "accuracy")),
    ("局限", ("limitation", "limitations", "weakness", "weaknesses", "caveat", "caveats")),
    ("限制", ("limitation", "limitations", "constraint", "constraints", "caveat")),
    ("不足", ("limitation", "limitations", "shortcoming", "shortcomings", "weakness")),
    ("缺点", ("limitation", "limitations", "weakness", "shortcoming")),
    ("复现", ("reproduction", "reproduce", "replication")),
    ("实现", ("implementation", "implementation")),
    ("训练", ("training", "train")),
    ("数据集", ("dataset", "data")),
    ("基线", ("baseline", "comparison")),
    ("对比", ("comparison", "baseline")),
    ("记忆", ("memory", "memorization")),
    ("代理", ("agent", "agents")),
    ("智能体", ("agent", "agents")),
    ("检索", ("retrieval", "search")),
    ("安全", ("safety", "security")),
    ("共识", ("consensus", "agreement")),
    ("近期", ("recent", "latest")),
    ("最新", ("recent", "latest")),
)


def expand_search_query(query: str) -> str:
    """Return a safe PostgreSQL web-search expression with bilingual aliases.

    English words already present in a question are preserved.  Chinese
    research terms add a small OR vocabulary, so ``方法有什么局限`` can match
    English ``method`` and ``limitation`` claims without an external model.
    """

    normalized = " ".join(query.split())
    terms = {
        word.casefold()
        for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,}", normalized)
        if word.casefold() not in _SEARCH_STOPWORDS
    }
    matched_alias = False
    for phrase, aliases in _CROSS_LANGUAGE_ALIASES:
        if phrase in normalized:
            matched_alias = True
            terms.update(aliases)
    if not terms or not matched_alias:
        # Keep the original query for non-Chinese or unknown-language input;
        # PostgreSQL will still apply its normal simple parser.
        return normalized
    return " OR ".join(sorted(terms))

def _json_dumps(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _durable_anchor(value: dict[str, Any]) -> DurableEvidenceAnchor:
    return DurableEvidenceAnchor(**value)
