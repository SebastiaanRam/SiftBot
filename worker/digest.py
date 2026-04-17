"""Digest builder and Telegram sender."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

if TYPE_CHECKING:
    from db.client import DBClient

COLD_START_THRESHOLD = 30

_INTRO_MESSAGE = (
    "📬 *Today's paper digest is ready!*\n\n"
    "_Rate each paper to help me learn your taste. "
    "After {threshold} ratings I'll start personalising your recommendations._"
)

_MILESTONE_MESSAGE = (
    "🎓 *I've learned enough to personalise your digest!*\n\n"
    "Your next digest will be ranked by your own preferences, not just topic keywords."
)


def _format_authors(authors: list[str]) -> str:
    if not authors:
        return "Unknown"
    shown = authors[:3]
    result = ", ".join(shown)
    if len(authors) > 3:
        result += " et al."
    return result


def _format_paper_message(paper: dict[str, Any]) -> str:
    """Format a paper dict (from DB) into a Telegram Markdown message."""
    title = paper.get("title", "Untitled")
    url = paper.get("url", "")
    authors = _format_authors(paper.get("authors") or [])
    source = paper.get("source", "")
    venue = paper.get("venue") or ""
    explanation = paper.get("llm_explanation") or ""
    score = paper.get("final_score", 0)

    source_line = source.replace("_", "\\_")
    if venue:
        source_line += f" · {venue}"

    msg = f"*[{title}]({url})*\n"
    msg += f"_{authors}_\n"
    msg += f"{source_line}\n"
    if explanation:
        msg += f"\n💡 {explanation}\n"
    msg += f"\n🔢 Relevance: {score:.1f}/10"
    return msg


def _build_keyboard(paper_id: str) -> InlineKeyboardMarkup:
    """Inline keyboard with 👎 👍 ❤️  — callback: rate|<paper_id>|<score>"""
    buttons = [
        InlineKeyboardButton("👎", callback_data=f"rate|{paper_id}|1"),
        InlineKeyboardButton("👍", callback_data=f"rate|{paper_id}|5"),
        InlineKeyboardButton("❤️", callback_data=f"rate|{paper_id}|10"),
    ]
    return InlineKeyboardMarkup([buttons])


async def send_digest(
    chat_id: int,
    papers: list[dict[str, Any]],
    bot: Bot,
    db: "DBClient",
    user_id: int,
    rating_count: int,
    dry_run: bool = False,
) -> None:
    """Send the digest to a single user. Logs each sent paper to digest_log."""
    if not papers:
        print(f"[digest] No papers to send to chat_id={chat_id}")
        return

    # Intro message
    intro = _INTRO_MESSAGE.format(threshold=COLD_START_THRESHOLD)
    if not dry_run:
        await bot.send_message(
            chat_id=chat_id,
            text=intro,
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        print(f"\n{'='*60}\nINTRO:\n{intro}")

    for paper in papers:
        text = _format_paper_message(paper)
        keyboard = _build_keyboard(paper["id"])

        if dry_run:
            print(f"\n{'─'*60}\n{text}\nCallback: rate|{paper['id']}|<score>")
        else:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
                db.log_sent(user_id, paper["id"])
            except Exception as exc:
                print(f"[digest] Failed to send paper {paper['id']}: {exc}")

    # Cold-start milestone: send once when user crosses the threshold
    if not dry_run and rating_count == COLD_START_THRESHOLD:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=_MILESTONE_MESSAGE,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            print(f"[digest] Failed to send milestone message: {exc}")

    print(f"[digest] Sent {len(papers)} papers to chat_id={chat_id}")
