"""Main daily digest entrypoint.

Usage:
    python -m worker.main                  # normal run (all active users)
    python -m worker.main --dry-run        # print digest, don't send
    python -m worker.main --chat-id 12345  # run for a single user by chat_id
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from telegram import Bot

from db.client import DBClient
from worker.fetch import Paper, deduplicate_new, fetch_all, title_hash
from worker.filter import ScoredPaper, score_papers
from worker.prefilter import PaperIndex
from worker.retrain import compute_pref_scores, load_model


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SiftBot daily digest")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the digest without sending to Telegram",
    )
    parser.add_argument(
        "--chat-id",
        type=int,
        default=None,
        help="Run for a single user by Telegram chat_id",
    )
    return parser.parse_args()


def _upsert_papers(papers: list[Paper], db: DBClient) -> None:
    for p in papers:
        db.upsert_paper(
            {
                "id": p.id,
                "title": p.title,
                "abstract": p.abstract,
                "authors": p.authors,
                "published_date": p.published.isoformat(),
                "source": p.source,
                "venue": p.venue,
                "url": p.url,
                "pdf_url": p.pdf_url,
                "title_hash": title_hash(p.title),
            }
        )


def _upsert_scores(
    scored: list[ScoredPaper],
    user_id: int,
    db: DBClient,
    pref_scores: dict[str, float] | None = None,
) -> None:
    for sp in scored:
        pref = (pref_scores or {}).get(sp.paper.id)
        db.upsert_score(
            paper_id=sp.paper.id,
            user_id=user_id,
            llm_score=sp.llm_score,
            llm_explanation=sp.llm_reason,
            pref_score=pref,
        )


async def run_for_user(
    user: dict,
    paper_index: PaperIndex,
    db: DBClient,
    bot: Bot,
    min_score: float,
    dry_run: bool,
) -> None:
    from worker.digest import send_digest

    user_id: int = user["id"]
    chat_id: int = user["chat_id"]
    profile: str = user.get("keyword_profile") or ""
    max_papers: int = user.get("max_papers") or int(os.environ.get("MAX_PAPERS_PER_DIGEST", "10"))
    days_back: int = int(os.environ.get("DAYS_BACK", "1"))
    top_n: int = int(os.environ.get("PREFILTER_TOP_N", "30"))

    if not profile:
        print(f"[main] Skipping user {chat_id}: no keyword_profile set")
        return

    print(f"\n[main] Processing user {chat_id} — profile: {profile[:80]}…")

    # ── Stage 1: cheap pre-filtering ──────────────────────────────────────
    model = load_model(user_id)
    if model is not None:
        candidates = paper_index.top_n_for_model(model, n=top_n)
        if not candidates:
            # Model prediction failed — fall back to keyword profile
            print(f"[main] Pref model returned no candidates; falling back to TF-IDF")
            model = None
            candidates = paper_index.top_n_for_profile(profile, n=top_n)
        else:
            print(f"[main] Pre-filtered to {len(candidates)} candidates via pref model")
    else:
        candidates = paper_index.top_n_for_profile(profile, n=top_n)
        print(f"[main] Pre-filtered to {len(candidates)} candidates via TF-IDF")

    if not candidates:
        print(f"[main] No candidates for user {chat_id}")
        return

    # ── Stage 2: LLM reranking on candidates only ────────────────────────
    if candidates and not dry_run:
        scored = score_papers(candidates, profile, min_score=min_score)
        if scored:
            paper_dicts = [
                {"id": sp.paper.id, "title": sp.paper.title, "abstract": sp.paper.abstract}
                for sp in scored
            ]
            pref_scores = compute_pref_scores(user_id, paper_dicts, model=model)
            if pref_scores:
                print(f"[main] Preference model provided scores for {len(pref_scores)} papers")
            _upsert_scores(scored, user_id, db, pref_scores)
    elif candidates and dry_run:
        scored = score_papers(candidates, profile, min_score=min_score)
    else:
        scored = []

    if dry_run:
        if not scored:
            print(f"[main] No relevant papers for user {chat_id}")
            return
        papers_to_send = [
            {
                "id": sp.paper.id,
                "title": sp.paper.title,
                "abstract": sp.paper.abstract,
                "authors": sp.paper.authors,
                "url": sp.paper.url,
                "pdf_url": sp.paper.pdf_url,
                "venue": sp.paper.venue,
                "source": sp.paper.source,
                "published_date": sp.paper.published.isoformat(),
                "final_score": sp.llm_score,
                "llm_explanation": sp.llm_reason,
            }
            for sp in sorted(scored, key=lambda x: x.llm_score, reverse=True)[:max_papers]
        ]
    else:
        papers_to_send = db.get_unsent_papers(user_id, min_score, max_papers, days_back)
        if not papers_to_send:
            print(f"[main] No unsent papers for user {chat_id}")
            return

    rating_count = db.count_ratings(chat_id) if not dry_run else 0

    await send_digest(
        chat_id=chat_id,
        papers=papers_to_send,
        bot=bot,
        db=db,
        user_id=user_id,
        rating_count=rating_count,
        dry_run=dry_run,
    )


async def main() -> None:
    load_dotenv()
    args = _parse_args()

    min_score = float(os.environ.get("MIN_RELEVANCE_SCORE", "6"))
    days_back = int(os.environ.get("DAYS_BACK", "1"))
    categories = os.environ.get("ARXIV_CATEGORIES", "cs.CV,eess.IV,cs.LG,q-bio.QM").split(",")
    s2_query = os.environ.get("S2_QUERY", "machine learning")
    pubmed_query = os.environ.get("PUBMED_QUERY", "")

    db = DBClient()
    bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"])

    if args.chat_id:
        user = db.get_user_by_chat_id(args.chat_id)
        if not user:
            print(f"[main] User with chat_id={args.chat_id} not found in database.")
            sys.exit(1)
        users = [user]
    else:
        users = db.get_active_users()

    print(f"[main] {len(users)} active user(s)")

    if not users:
        print("[main] No active users. Exiting.")
        return

    # Fetch papers once, then filter per user
    all_papers = fetch_all(categories, s2_query, pubmed_query, days_back)
    print(f"[main] Total papers fetched: {len(all_papers)}")

    if not all_papers:
        print("[main] No papers fetched today. Exiting.")
        return

    # Upsert all papers to DB (so they're available for the unsent query)
    if not args.dry_run:
        new_papers = deduplicate_new(all_papers, db)
        print(f"[main] New papers (not in DB yet): {len(new_papers)}")
        _upsert_papers(new_papers, db)
    else:
        new_papers = all_papers  # In dry-run, skip DB entirely

    # Build TF-IDF index once, shared across all users
    paper_index = PaperIndex(all_papers)
    print(f"[main] Built paper index over {len(all_papers)} papers")

    for user in users:
        await run_for_user(
            user=user,
            paper_index=paper_index,
            db=db,
            bot=bot,
            min_score=min_score,
            dry_run=args.dry_run,
        )

    print("\n[main] Done.")


if __name__ == "__main__":
    asyncio.run(main())
