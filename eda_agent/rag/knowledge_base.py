"""
knowledge_base.py — Semantic search over markdown knowledge bases.

Splits a markdown file into sections at ## and ### headings, embeds them
using sentence-transformers, and builds a FAISS index for fast cosine
similarity search. Results are cached to disk (keyed by content hash)
for instant startup on subsequent runs.

Usage:
    from eda_agent.rag import KnowledgeBase
    kb = KnowledgeBase("path/to/knowledge_base.md")
    context = kb.retrieve("how to create a testbench", top_k=3)
"""

import hashlib
import pickle
import re
from pathlib import Path

import faiss
import numpy as np

# ── Lazy model loading ────────────────────────────────────────────────────

_model = None
_MODEL_NAME = "all-MiniLM-L6-v2"


def _get_model():
    """Load the sentence-transformer model lazily on first use."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


# ── Data classes ──────────────────────────────────────────────────────────

class KBSection:
    """A single retrievable section from the knowledge base.

    Attributes:
        title: Section heading text (e.g. "9.3 Running Simulations").
        level: Heading depth (2 = ##, 3 = ###).
        content: Full markdown text of this section.
        path: Breadcrumb path (e.g. "9. SPECTRE > 9.3 Running Simulations").
    """

    __slots__ = ("title", "level", "content", "path")

    def __init__(self, title: str, level: int, content: str, path: str):
        self.title = title
        self.level = level
        self.content = content
        self.path = path


# ── Main class ────────────────────────────────────────────────────────────

class KnowledgeBase:
    """Semantic search over a markdown knowledge base using FAISS.

    On first load, embeds all sections and caches the FAISS index to disk
    (keyed by MD5 of the file) for instant startup on subsequent runs.

    Args:
        md_path: Path to the markdown knowledge base file.
        cache_dir: Directory for FAISS cache files. Defaults to same dir as md_path.
    """

    def __init__(self, md_path: str, cache_dir: str | None = None):
        self.md_path = Path(md_path).resolve()
        self.cache_dir = Path(cache_dir) if cache_dir else self.md_path.parent
        self.sections: list[KBSection] = []
        self.index: faiss.IndexFlatIP | None = None
        self._load_and_index()

    # ── Public API ────────────────────────────────────────────────────────

    def retrieve(self, query: str, top_k: int = 3) -> str:
        """Retrieve the top-K most relevant sections for a query.

        Returns a formatted string ready for prompt injection, or "" if
        no relevant results are found.
        """
        if not self.sections or self.index is None:
            return ""

        model = _get_model()
        q_vec = model.encode([query], normalize_embeddings=True)
        scores, indices = self.index.search(
            np.array(q_vec, dtype=np.float32), min(top_k, len(self.sections))
        )

        # Filter by relevance threshold, deduplicate by title
        results = []
        seen = set()
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or score < 0.15:
                continue
            sec = self.sections[idx]
            if sec.title in seen:
                continue
            seen.add(sec.title)
            results.append(sec)

        if not results:
            return ""

        # Format for prompt injection
        parts = ["[RELEVANT DOCUMENTATION — from virtuoso-bridge-lite knowledge base]"]
        for sec in results:
            content = sec.content
            if len(content) > 3000:
                content = content[:3000] + "\n... (truncated)"
            parts.append(f"\n### {sec.path}\n{content}")
        parts.append("[END DOCUMENTATION]")

        return "\n".join(parts)

    def retrieve_for_error(self, error_text: str, top_k: int = 2) -> str:
        """Retrieve sections relevant to a specific error, biased toward troubleshooting."""
        return self.retrieve(f"troubleshooting error: {error_text}", top_k=top_k)

    def search(self, query: str, top_k: int = 5) -> list[tuple[float, KBSection]]:
        """Return scored (similarity, section) tuples for inspection/debugging."""
        if not self.sections or self.index is None:
            return []

        model = _get_model()
        q_vec = model.encode([query], normalize_embeddings=True)
        scores, indices = self.index.search(
            np.array(q_vec, dtype=np.float32), min(top_k, len(self.sections))
        )

        return [
            (float(s), self.sections[i])
            for s, i in zip(scores[0], indices[0])
            if i >= 0
        ]

    # ── Internals ─────────────────────────────────────────────────────────

    def _load_and_index(self):
        """Load markdown, split into sections, embed, and build FAISS index."""
        md_text = self.md_path.read_text(encoding="utf-8")
        self.sections = self._split_sections(md_text)

        if not self.sections:
            return

        # Try loading from cache (keyed by content hash)
        md_hash = hashlib.md5(md_text.encode()).hexdigest()
        cache_path = self.cache_dir / f".kb_cache_{md_hash}.pkl"

        if cache_path.exists():
            try:
                with open(cache_path, "rb") as f:
                    cached = pickle.load(f)
                self.index = cached["index"]
                if self.index.ntotal == len(self.sections):
                    return  # cache hit — skip embedding
            except Exception:
                pass  # fall through to re-index

        # Embed all sections and build FAISS index
        model = _get_model()
        texts = [f"{s.path}\n{s.content[:1000]}" for s in self.sections]
        embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=True)
        embeddings = np.array(embeddings, dtype=np.float32)

        # Inner product on normalized vectors = cosine similarity
        self.index = faiss.IndexFlatIP(embeddings.shape[1])
        self.index.add(embeddings)

        # Cache to disk for fast reload
        try:
            with open(cache_path, "wb") as f:
                pickle.dump({"index": self.index}, f)
        except OSError:
            pass  # non-fatal — just slower next startup

    @staticmethod
    def _split_sections(md_text: str) -> list[KBSection]:
        """Split markdown into sections at ## and ### boundaries.

        Each section includes its heading + body text until the next heading
        of equal or higher level. Sections shorter than 50 chars are skipped.
        """
        lines = md_text.split("\n")
        sections: list[KBSection] = []
        current_h2 = ""
        current_title = ""
        current_level = 0
        current_lines: list[str] = []

        heading_re = re.compile(r"^(#{2,3})\s+(.+)$")

        def flush():
            if current_title and current_lines:
                content = "\n".join(current_lines).strip()
                if len(content) > 50:  # skip near-empty sections
                    if current_level == 3 and current_h2:
                        path = f"{current_h2} > {current_title}"
                    else:
                        path = current_title
                    sections.append(KBSection(current_title, current_level, content, path))

        for line in lines:
            m = heading_re.match(line)
            if m:
                flush()
                level = len(m.group(1))  # 2 or 3
                title = m.group(2).strip()
                if level == 2:
                    current_h2 = title
                current_title = title
                current_level = level
                current_lines = [line]
            else:
                current_lines.append(line)

        flush()
        return sections


# ── CLI test ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m eda_agent.rag.knowledge_base <path_to_kb.md>")
        sys.exit(1)

    kb_path = sys.argv[1]
    print(f"Loading knowledge base from {kb_path}...")
    kb = KnowledgeBase(kb_path)
    print(f"Indexed {len(kb.sections)} sections.\n")

    test_queries = [
        "how to create a schematic",
        "spectre simulation",
        "layout via creation",
    ]
    for q in test_queries:
        print(f"─── Query: {q} ───")
        for score, sec in kb.search(q, top_k=3):
            print(f"  [{score:.3f}] {sec.path}")
        print()
