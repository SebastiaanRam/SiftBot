"""Supabase database client wrapper."""

from __future__ import annotations

import os
from typing import Any

from supabase import create_client, Client


def get_client() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    return create_client(url, key)


class DBClient:
    def __init__(self) -> None:
        self._db: Client = get_client()

    # ── Users ──────────────────────────────────────────────────────────────────

    def get_active_users(self) -> list[dict[str, Any]]:
        """Return all active users."""
        res = self._db.table("users").select("*").eq("active", True).execute()
        return res.data

    def get_user_by_chat_id(self, chat_id: int) -> dict[str, Any] | None:
        res = (
            self._db.table("users")
            .select("*")
            .eq("chat_id", chat_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    def upsert_user(self, chat_id: int, **kwargs: Any) -> dict[str, Any]:
        """Insert or update a user row. Returns the upserted row."""
        data = {"chat_id": chat_id, **kwargs}
        res = (
            self._db.table("users")
            .upsert(data, on_conflict="chat_id")
            .execute()
        )
        return res.data[0]

    def set_user_field(self, chat_id: int, **kwargs: Any) -> None:
        self._db.table("users").update(kwargs).eq("chat_id", chat_id).execute()

    # ── Papers ─────────────────────────────────────────────────────────────────

    def paper_exists(self, title_hash: str) -> bool:
        res = (
            self._db.table("papers")
            .select("id")
            .eq("title_hash", title_hash)
            .limit(1)
            .execute()
        )
        return bool(res.data)

    def upsert_paper(self, paper_dict: dict[str, Any]) -> None:
        self._db.table("papers").upsert(paper_dict, on_conflict="id").execute()

    # ── Scores ─────────────────────────────────────────────────────────────────

    def upsert_score(
        self,
        paper_id: str,
        user_id: int,
        llm_score: float,
        llm_explanation: str,
        pref_score: float | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "paper_id": paper_id,
            "user_id": user_id,
            "llm_score": llm_score,
            "llm_explanation": llm_explanation,
        }
        if pref_score is not None:
            data["pref_score"] = pref_score
        self._db.table("paper_scores").upsert(
            data, on_conflict="paper_id,user_id"
        ).execute()

    def update_pref_scores(self, user_id: int, scores: dict[str, float]) -> None:
        """Bulk-update pref_score for the given paper_ids."""
        for paper_id, pref_score in scores.items():
            self._db.table("paper_scores").update({"pref_score": pref_score}).eq(
                "paper_id", paper_id
            ).eq("user_id", user_id).execute()

    # ── Digest ─────────────────────────────────────────────────────────────────

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

        # Get scored papers for this user above min_score
        scores_res = (
            self._db.table("paper_scores")
            .select("paper_id, final_score, llm_score, llm_explanation")
            .eq("user_id", user_id)
            .gte("final_score", min_score)
            .order("final_score", desc=True)
            .limit(max_papers * 5)  # fetch extra, filter below
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

        return results

    def log_sent(self, user_id: int, paper_id: str) -> None:
        self._db.table("digest_log").upsert(
            {"user_id": user_id, "paper_id": paper_id},
            on_conflict="user_id,paper_id",
        ).execute()

    # ── Ratings / training ─────────────────────────────────────────────────────

    def count_ratings(self, chat_id: int) -> int:
        res = (
            self._db.table("ratings")
            .select("id", count="exact")
            .eq("user_chat_id", chat_id)
            .execute()
        )
        return res.count or 0

    def get_ratings_for_training(self, chat_id: int) -> list[dict[str, Any]]:
        """Return ratings joined with paper text for model training."""
        res = (
            self._db.table("ratings")
            .select("paper_id, score, papers(title, abstract), paper_scores(llm_score)")
            .eq("user_chat_id", chat_id)
            .execute()
        )
        rows = []
        for row in res.data:
            paper = row.get("papers") or {}
            score_info = row.get("paper_scores") or {}
            if isinstance(score_info, list):
                score_info = score_info[0] if score_info else {}
            rows.append(
                {
                    "paper_id": row["paper_id"],
                    "score": row["score"],
                    "title": paper.get("title", ""),
                    "abstract": paper.get("abstract", ""),
                    "llm_score": score_info.get("llm_score"),
                }
            )
        return rows
