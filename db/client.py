"""Supabase database client wrapper."""

from __future__ import annotations

import os
import time
from functools import wraps
from typing import Any

from postgrest.exceptions import APIError
from supabase import create_client, Client


def _is_transient(exc: APIError) -> bool:
    """Gateway/proxy hiccups (504, 502, 503) that are worth retrying."""
    code = str(getattr(exc, "code", "") or "")
    message = str(getattr(exc, "message", "") or "")
    return code in {"502", "503", "504"} or "gateway timeout" in message.lower()


def _retry_transient(retries: int = 3, base_delay: float = 1.0):
    """Retry a DB call a few times on transient PostgREST errors (e.g. 504s)."""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(retries + 1):
                try:
                    return func(*args, **kwargs)
                except APIError as e:
                    if attempt == retries or not _is_transient(e):
                        raise
                    time.sleep(delay)
                    delay *= 2

        return wrapper

    return decorator


class DBClient:
    def __init__(self) -> None:
        self._db: Client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

    # ── Users ──────────────────────────────────────────────────────────────────

    @_retry_transient()
    def get_active_users(self) -> list[dict[str, Any]]:
        """Return all active users."""
        res = self._db.table("users").select("*").eq("active", True).execute()
        return res.data

    @_retry_transient()
    def get_user_by_chat_id(self, chat_id: int) -> dict[str, Any] | None:
        res = (
            self._db.table("users")
            .select("*")
            .eq("chat_id", chat_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    @_retry_transient()
    def upsert_user(self, chat_id: int, **kwargs: Any) -> dict[str, Any]:
        """Insert or update a user row. Returns the upserted row."""
        data = {"chat_id": chat_id, **kwargs}
        res = (
            self._db.table("users")
            .upsert(data, on_conflict="chat_id")
            .execute()
        )
        return res.data[0]

    @_retry_transient()
    def set_user_field(self, chat_id: int, **kwargs: Any) -> None:
        self._db.table("users").update(kwargs).eq("chat_id", chat_id).execute()

    # ── Papers ─────────────────────────────────────────────────────────────────

    @_retry_transient()
    def get_existing_title_hashes(self, title_hashes: list[str]) -> set[str]:
        """Return the subset of the given title_hashes already present in the DB.

        Checked in one batched query (chunked) instead of one round-trip per
        paper, since fetching them one at a time made a full digest run do
        ~100 sequential Supabase calls and any single transient timeout
        aborted the whole run.
        """
        existing: set[str] = set()
        chunk_size = 200
        for i in range(0, len(title_hashes), chunk_size):
            chunk = title_hashes[i : i + chunk_size]
            res = (
                self._db.table("papers")
                .select("title_hash")
                .in_("title_hash", chunk)
                .execute()
            )
            existing.update(row["title_hash"] for row in res.data)
        return existing

    @_retry_transient()
    def upsert_paper(self, paper_dict: dict[str, Any]) -> None:
        self._db.table("papers").upsert(paper_dict, on_conflict="id").execute()

    # ── Scores ─────────────────────────────────────────────────────────────────

    @_retry_transient()
    def upsert_score(
        self,
        paper_id: str,
        user_id: int,
        llm_score: float | None = None,
        llm_explanation: str | None = None,
        pref_score: float | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "paper_id": paper_id,
            "user_id": user_id,
        }
        if llm_score is not None:
            data["llm_score"] = llm_score
        if llm_explanation is not None:
            data["llm_explanation"] = llm_explanation
        if pref_score is not None:
            data["pref_score"] = pref_score
        self._db.table("paper_scores").upsert(
            data, on_conflict="paper_id,user_id"
        ).execute()

    # ── Digest ─────────────────────────────────────────────────────────────────

    @_retry_transient()
    def get_unsent_papers(
        self,
        user_id: int,
        min_score: float,
        max_papers: int,
        days_back: int = 2,
    ) -> list[dict[str, Any]]:
        """Return papers scored for this user that haven't been sent yet."""
        return self._get_unsent_papers_fallback(user_id, min_score, max_papers, days_back)

    def _get_unsent_papers_fallback(
        self,
        user_id: int,
        min_score: float,
        max_papers: int,
        days_back: int,
    ) -> list[dict[str, Any]]:
        """Fetch unsent papers by joining scores and digest_log in Python."""
        from datetime import date, timedelta

        cutoff = (date.today() - timedelta(days=days_back)).isoformat()

        # Get all paper_ids already sent
        sent_res = (
            self._db.table("digest_log")
            .select("paper_id")
            .eq("user_id", user_id)
            .execute()
        )
        sent_ids = {row["paper_id"] for row in sent_res.data}

        # Get scored papers for this user above min_score, most recently scored
        # first so today's papers aren't crowded out by older high-scoring ones.
        scores_res = (
            self._db.table("paper_scores")
            .select("paper_id, final_score, llm_score, llm_explanation, scored_at")
            .eq("user_id", user_id)
            .gte("final_score", min_score)
            .order("scored_at", desc=True)
            .order("final_score", desc=True)
            .limit(max_papers * 10)  # fetch extra, filter below
            .execute()
        )

        results = []
        for score_row in scores_res.data:
            if score_row["paper_id"] in sent_ids:
                continue
            # Fetch paper metadata
            paper_res = (
                self._db.table("papers")
                .select("id, title, abstract, authors, url, pdf_url, venue, source, published_date")
                .eq("id", score_row["paper_id"])
                .gte("published_date", cutoff)
                .limit(1)
                .execute()
            )
            if not paper_res.data:
                continue
            paper = paper_res.data[0]
            paper["final_score"] = score_row["final_score"]
            paper["llm_explanation"] = score_row["llm_explanation"]
            results.append(paper)
            if len(results) >= max_papers:
                break

        results.sort(key=lambda p: p["final_score"], reverse=True)
        return results

    @_retry_transient()
    def log_sent(self, user_id: int, paper_id: str) -> None:
        self._db.table("digest_log").upsert(
            {"user_id": user_id, "paper_id": paper_id},
            on_conflict="user_id,paper_id",
        ).execute()

    # ── Ratings / training ─────────────────────────────────────────────────────

    @_retry_transient()
    def count_ratings(self, chat_id: int) -> int:
        res = (
            self._db.table("ratings")
            .select("id", count="exact")
            .eq("user_chat_id", chat_id)
            .execute()
        )
        return res.count or 0

    @_retry_transient()
    def get_ratings_for_training(self, chat_id: int) -> list[dict[str, Any]]:
        """Return ratings joined with paper text for model training."""
        res = (
            self._db.table("ratings")
            .select("paper_id, score, papers(title, abstract)")
            .eq("user_chat_id", chat_id)
            .execute()
        )
        rows = []
        for row in res.data:
            paper = row.get("papers") or {}
            rows.append(
                {
                    "paper_id": row["paper_id"],
                    "score": row["score"],
                    "title": paper.get("title", ""),
                    "abstract": paper.get("abstract", ""),
                }
            )
        return rows
