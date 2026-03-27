"""Weekly preference model retraining."""

from __future__ import annotations

import os
import pathlib
from typing import Any

import joblib
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_score
from sklearn.pipeline import Pipeline

from db.client import DBClient

MIN_RATINGS = 30
MODEL_DIR = pathlib.Path("model")


def _build_training_data(
    rows: list[dict[str, Any]],
) -> tuple[list[str], list[int]]:
    """Convert DB rows to texts and binary labels (1=liked, 0=disliked)."""
    texts: list[str] = []
    labels: list[int] = []
    for row in rows:
        text = f"{row.get('title', '')} {row.get('abstract', '')}".strip()
        score = row.get("score", 0)
        if score >= 5:
            labels.append(1)
        else:
            labels.append(0)
        texts.append(text)
    return texts, labels


def train_for_user(user_id: int, chat_id: int, db: DBClient) -> bool:
    """Train and save a preference model for one user. Returns True on success."""
    rows = db.get_ratings_for_training(chat_id)
    if len(rows) < MIN_RATINGS:
        print(f"[retrain] user {user_id} has only {len(rows)} ratings — skipping (need {MIN_RATINGS})")
        return False

    texts, labels = _build_training_data(rows)
    if len(set(labels)) < 2:
        print(f"[retrain] user {user_id} has only one label class — skipping")
        return False

    pipeline = Pipeline(
        [
            (
                "tfidf",
                TfidfVectorizer(
                    max_features=5000,
                    ngram_range=(1, 2),
                    sublinear_tf=True,
                    stop_words="english",
                ),
            ),
            (
                "clf",
                LogisticRegression(C=1.0, max_iter=1000, class_weight="balanced"),
            ),
        ]
    )

    # Cross-validate
    scores = cross_val_score(pipeline, texts, labels, cv=5, scoring="roc_auc")
    print(
        f"[retrain] user {user_id}: CV AUC = {scores.mean():.3f} ± {scores.std():.3f} "
        f"(n={len(rows)})"
    )

    # Fit on full data
    pipeline.fit(texts, labels)

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model_path = MODEL_DIR / f"{user_id}_model.pkl"
    joblib.dump(pipeline, model_path)
    print(f"[retrain] Saved model → {model_path}")
    return True


def load_model(user_id: int) -> Pipeline | None:
    """Load a trained pipeline for a user, or None if not available."""
    model_path = MODEL_DIR / f"{user_id}_model.pkl"
    if not model_path.exists():
        return None
    try:
        return joblib.load(model_path)
    except Exception as exc:
        print(f"[retrain] Failed to load model for user {user_id}: {exc}")
        return None


def compute_pref_scores(
    user_id: int, papers: list[dict[str, Any]]
) -> dict[str, float]:
    """Return a dict mapping paper_id → pref_score (0–10) for scored papers."""
    model = load_model(user_id)
    if model is None or not papers:
        return {}

    texts = [f"{p.get('title', '')} {p.get('abstract', '')}".strip() for p in papers]
    try:
        proba = model.predict_proba(texts)[:, 1]
    except Exception as exc:
        print(f"[retrain] Prediction failed for user {user_id}: {exc}")
        return {}

    return {p["id"]: float(prob * 10) for p, prob in zip(papers, proba)}


def retrain_all() -> None:
    """Main entry point: retrain models for all eligible users."""
    from dotenv import load_dotenv

    load_dotenv()
    db = DBClient()
    users = db.get_active_users()
    print(f"[retrain] Found {len(users)} active users")

    success = 0
    for user in users:
        ok = train_for_user(user["id"], user["chat_id"], db)
        if ok:
            success += 1

    print(f"[retrain] Trained {success}/{len(users)} models")


if __name__ == "__main__":
    retrain_all()
