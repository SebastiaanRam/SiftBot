"""Cheap pre-filtering using TF-IDF cosine similarity.

Builds a TF-IDF index over all papers once per run, then supports
fast per-user candidate retrieval via keyword-profile similarity
(cold-start) or a trained preference model (personalised).
"""

from __future__ import annotations

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.pipeline import Pipeline

from worker.fetch import Paper


class PaperIndex:
    """Vectorises papers once; supports fast per-user filtering."""

    def __init__(self, papers: list[Paper]) -> None:
        self._papers = papers
        self._texts = [f"{p.title} {p.abstract}" for p in papers]
        self._vectorizer = TfidfVectorizer(
            max_features=5000,
            ngram_range=(1, 2),
            sublinear_tf=True,
            stop_words="english",
        )
        self._paper_matrix = self._vectorizer.fit_transform(self._texts)

    def top_n_for_profile(self, keyword_profile: str, n: int = 30) -> list[Paper]:
        """Cold-start: rank papers by cosine similarity to keyword profile."""
        profile_vec = self._vectorizer.transform([keyword_profile])
        sims = cosine_similarity(profile_vec, self._paper_matrix).flatten()
        top_indices = sims.argsort()[::-1][:n]
        return [self._papers[i] for i in top_indices]

    def top_n_for_model(
        self, model: Pipeline, n: int = 30,
    ) -> list[Paper]:
        """Trained users: rank by preference model probability, return top n."""
        try:
            proba = model.predict_proba(self._texts)[:, 1]
        except Exception as exc:
            print(f"[prefilter] predict_proba failed: {exc}")
            return []
        top_indices = np.argsort(proba)[::-1][:n]
        return [self._papers[i] for i in top_indices]
