"""Base de connaissances (RAG) : runbooks de knowledge/, recherchés par mots-clés (BM25).

La requête est construite en Python à partir des preuves mesurées, jamais par le LLM.
BM25 suffit pour quelques dizaines de documents : pas de modèle d'embeddings, pas de
service en plus, et un résultat toujours identique (donc testable).
"""

import math
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from app.schemas.diagnosis import Document, Finding

MAX_DOCUMENTS = 2
_WORD = re.compile(r"\w{3,}")
# Une preuve « stable » ou « au même rythme » décrit ce qui n'a PAS changé : elle
# orienterait la recherche vers une fausse piste (ex. « cpu_usage : stable »).
_UNCHANGED = ("stable", "même rythme")


def load_documents(root: Path) -> list[Document]:
    """Un document par fichier .md (README exclu), titre = première ligne « # ... »."""
    if not root.is_dir():
        return []
    documents = []
    for path in sorted(root.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        text = path.read_text(encoding="utf-8").strip()
        title = text.splitlines()[0].lstrip("# ").strip() if text else path.stem
        documents.append(Document(id="doc-000", title=title, path=path.name, text=text))
    return documents


def build_query(findings: Sequence[Finding]) -> str:
    """Services et observations des preuves qui ont changé pendant l'incident."""
    parts = [
        f"{e.service} {e.observation}"
        for f in findings
        for e in f.evidences
        if not any(word in e.observation for word in _UNCHANGED)
    ]
    return " ".join(parts)


def search(documents: Sequence[Document], query: str, k: int = MAX_DOCUMENTS) -> list[Document]:
    """Les k documents les plus pertinents (score BM25 > 0), numérotés doc-001, doc-002..."""
    terms = set(_words(query))
    if not documents or not terms:
        return []
    texts = [_words(d.text) for d in documents]
    avg_len = sum(map(len, texts)) / len(texts)
    df = Counter(word for words in texts for word in set(words))

    def score(words: list[str]) -> float:
        tf = Counter(words)
        total = 0.0
        for term in terms & tf.keys():
            idf = math.log(1 + (len(texts) - df[term] + 0.5) / (df[term] + 0.5))
            total += idf * tf[term] * 2.2 / (tf[term] + 1.2 * (0.25 + 0.75 * len(words) / avg_len))
        return total

    ranked = sorted(zip(documents, map(score, texts), strict=True), key=lambda p: -p[1])
    found = [doc for doc, s in ranked[:k] if s > 0]
    return [d.model_copy(update={"id": f"doc-{i:03d}"}) for i, d in enumerate(found, start=1)]


def _words(text: str) -> list[str]:
    # « QueuePool » et « db.acquire_connection » deviennent « queuepool », « acquire_connection ».
    return _WORD.findall(text.lower())
