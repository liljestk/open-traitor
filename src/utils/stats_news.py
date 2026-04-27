"""News article persistence + pgvector embeddings.

Owns the ``news_articles`` table that promotes news from ephemeral
Redis-only state to a durable, searchable corpus. Each row stores the
original article fields plus a pgvector embedding so the system can:

* Deduplicate near-identical headlines across sources.
* Surface the most semantically similar historical articles for any new
  catalyst (used by the cross-asset narrator to ground LLM output).
* Backfill long-running embeddings without losing any data: rows with
  ``embedding IS NULL`` are picked up by ``backfill_news_embeddings.py``.

Embedding dimensionality is governed by ``src/utils/embeddings.py``
(``NEWS_EMBED_DIM``). The DDL inlines the integer literal because
PostgreSQL ``vector(N)`` requires a constant; an assertion at class load
keeps the two in sync.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Optional

import psycopg2
import psycopg2.extras

from src.utils.embeddings import NEWS_EMBED_DIM
from src.utils.logger import get_logger

logger = get_logger("stats.news")


# Hard-coded into the DDL string below; assertion in _init_news_schema()
# keeps DDL <-> embeddings.NEWS_EMBED_DIM in sync.
_NEWS_DDL_DIM = 768


def _vec_literal(vec: list[float]) -> str:
    """Format a float list as a pgvector text literal."""
    return "[" + ",".join(f"{float(x):.7g}" for x in vec) + "]"


class NewsMixin:
    """Mixin owning the news_articles table and pgvector search."""

    _NEWS_DDL_STATEMENTS: tuple[str, ...] = (
        # Extension is also created by PatternsMixin; idempotent.
        "CREATE EXTENSION IF NOT EXISTS vector",
        f"""
        CREATE TABLE IF NOT EXISTS news_articles (
            id                TEXT PRIMARY KEY,
            profile           TEXT NOT NULL DEFAULT 'default',
            source            TEXT NOT NULL,
            title             TEXT NOT NULL,
            summary           TEXT NOT NULL DEFAULT '',
            url               TEXT NOT NULL DEFAULT '',
            published         TIMESTAMPTZ,
            sentiment         TEXT,
            relevance_score   REAL NOT NULL DEFAULT 0.0,
            tickers           JSONB NOT NULL DEFAULT '[]'::jsonb,
            embedding         vector({_NEWS_DDL_DIM}),
            embedded_at       TIMESTAMPTZ,
            inserted_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_news_profile_published "
        "ON news_articles(profile, published DESC NULLS LAST)",
        "CREATE INDEX IF NOT EXISTS idx_news_profile_relevance "
        "ON news_articles(profile, relevance_score DESC)",
        "CREATE INDEX IF NOT EXISTS idx_news_missing_embed "
        "ON news_articles(profile, inserted_at) WHERE embedding IS NULL",
        # IVFFLAT cosine index — created lazily once the table has rows.
        # Skipping it on an empty table avoids the "training requires at
        # least 1 row" warning some pgvector versions emit.
        "CREATE INDEX IF NOT EXISTS idx_news_embedding_cos "
        "ON news_articles USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)",
    )

    def _init_news_schema(self) -> None:
        """Create news_articles table. Safe to call at every startup."""
        assert NEWS_EMBED_DIM == _NEWS_DDL_DIM, (
            f"NEWS_EMBED_DIM={NEWS_EMBED_DIM} but DDL is vector({_NEWS_DDL_DIM}). "
            "Drop and recreate news_articles.embedding to change dim."
        )
        with self._get_conn() as conn:
            for stmt in self._NEWS_DDL_STATEMENTS:
                with conn.cursor() as cur:
                    try:
                        cur.execute("SAVEPOINT news_ddl_sp")
                        cur.execute(stmt)
                        cur.execute("RELEASE SAVEPOINT news_ddl_sp")
                    except psycopg2.errors.InsufficientPrivilege as e:
                        cur.execute("ROLLBACK TO SAVEPOINT news_ddl_sp")
                        logger.warning(
                            f"News DDL skipped — insufficient privilege: {e}"
                        )
                    except psycopg2.Error as e:
                        cur.execute("ROLLBACK TO SAVEPOINT news_ddl_sp")
                        logger.warning(
                            f"News DDL failed: {stmt[:60]}…  → {e}"
                        )
            conn.commit()

    # ─── upsert ──────────────────────────────────────────────────────────

    def upsert_news_articles(
        self,
        articles: Iterable[dict[str, Any]],
        *,
        profile: str = "default",
    ) -> int:
        """Idempotently insert/update news rows. Embeddings are optional.

        Each row dict requires ``id``, ``source``, ``title``. Other fields
        are best-effort. ``embedding`` may be omitted; backfill picks it
        up later. Returns the count of rows actually written.
        """
        rows = list(articles)
        if not rows:
            return 0
        sql = """
            INSERT INTO news_articles
                (id, profile, source, title, summary, url, published,
                 sentiment, relevance_score, tickers, embedding, embedded_at)
            VALUES %s
            ON CONFLICT (id) DO UPDATE SET
                profile         = EXCLUDED.profile,
                source          = EXCLUDED.source,
                title           = EXCLUDED.title,
                summary         = EXCLUDED.summary,
                url             = EXCLUDED.url,
                published       = COALESCE(EXCLUDED.published, news_articles.published),
                sentiment       = COALESCE(EXCLUDED.sentiment, news_articles.sentiment),
                relevance_score = GREATEST(EXCLUDED.relevance_score, news_articles.relevance_score),
                tickers         = EXCLUDED.tickers,
                embedding       = COALESCE(EXCLUDED.embedding, news_articles.embedding),
                embedded_at     = COALESCE(EXCLUDED.embedded_at, news_articles.embedded_at)
        """
        values = []
        for r in rows:
            emb = r.get("embedding")
            emb_lit = _vec_literal(emb) if emb else None
            embedded_at = (
                r.get("embedded_at")
                or (datetime.utcnow() if emb_lit else None)
            )
            tickers_raw = r.get("tickers") or r.get("tags") or []
            try:
                tickers_json = json.dumps(list(tickers_raw))
            except Exception:
                tickers_json = "[]"
            values.append((
                str(r.get("id") or ""),
                profile,
                str(r.get("source") or "unknown"),
                str(r.get("title") or "")[:1000],
                str(r.get("summary") or "")[:4000],
                str(r.get("url") or "")[:1000],
                r.get("published"),
                r.get("sentiment"),
                float(r.get("relevance_score") or 0.0),
                tickers_json,
                emb_lit,
                embedded_at,
            ))
        # psycopg2.extras.execute_values can't render the pgvector literal
        # natively, so use a template that wraps the embedding in a CAST.
        template = (
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,"
            "CASE WHEN %s IS NULL THEN NULL ELSE %s::vector END,%s)"
        )
        # Insert the embedding literal twice so the CASE has both arms.
        expanded = [
            (vid, prof, src, title, summ, url, pub, sent, rel,
             tjson, emb_lit, emb_lit, emb_at)
            for (vid, prof, src, title, summ, url, pub, sent, rel,
                 tjson, emb_lit, emb_at) in values
        ]
        with self._get_conn() as conn, conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur, sql, expanded, template=template, page_size=200,
            )
            conn.commit()
        return len(values)

    # ─── reads ───────────────────────────────────────────────────────────

    def get_news_articles_missing_embedding(
        self, *, profile: Optional[str] = None, limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return rows whose embedding is NULL, oldest first.

        ``profile=None`` reads across all profiles (used by the operator
        backfill script). Trading-loop callers should always pass an
        explicit profile per the domain-separation rule.
        """
        sql = (
            "SELECT id, profile, source, title, summary "
            "FROM news_articles WHERE embedding IS NULL"
        )
        params: list[Any] = []
        if profile is not None:
            sql += " AND profile = %s"
            params.append(profile)
        sql += " ORDER BY inserted_at ASC LIMIT %s"
        params.append(int(limit))
        with self._get_conn() as conn, conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def update_news_embedding(
        self, article_id: str, embedding: list[float],
    ) -> int:
        """Set the embedding for a single article. Returns row count."""
        if not article_id or not embedding:
            return 0
        if len(embedding) != NEWS_EMBED_DIM:
            raise ValueError(
                f"embedding dim {len(embedding)} != {NEWS_EMBED_DIM}"
            )
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE news_articles "
                "SET embedding = %s::vector, embedded_at = now() "
                "WHERE id = %s",
                (_vec_literal(embedding), article_id),
            )
            conn.commit()
            return cur.rowcount

    def search_similar_news(
        self,
        query_vector: list[float],
        *,
        profile: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Cosine-ANN search for articles most similar to ``query_vector``."""
        if len(query_vector) != NEWS_EMBED_DIM:
            raise ValueError(
                f"query_vector dim {len(query_vector)} != {NEWS_EMBED_DIM}"
            )
        sql = (
            "SELECT id, profile, source, title, summary, url, published, "
            "       sentiment, relevance_score, tickers, "
            "       1 - (embedding <=> %s::vector) AS similarity "
            "FROM news_articles WHERE embedding IS NOT NULL"
        )
        params: list[Any] = [_vec_literal(query_vector)]
        if profile is not None:
            sql += " AND profile = %s"
            params.append(profile)
        sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
        params.append(_vec_literal(query_vector))
        params.append(int(limit))
        with self._get_conn() as conn, conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def count_news_articles(
        self, *, profile: Optional[str] = None,
    ) -> tuple[int, int]:
        """Return ``(total, with_embedding)`` for monitoring."""
        sql = "SELECT count(*) AS t, count(embedding) AS e FROM news_articles"
        params: list[Any] = []
        if profile is not None:
            sql += " WHERE profile = %s"
            params.append(profile)
        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return int(row[0] or 0), int(row[1] or 0)
