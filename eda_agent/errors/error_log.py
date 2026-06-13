"""
error_log.py — Persistent error logging with semantic search.

Records runtime errors to a JSONL file (one entry per line). When a new
error occurs, it checks if a semantically similar error already exists
(cosine similarity >= 0.85) and increments its frequency counter instead
of duplicating. Resolved errors (with their fix code) are retrievable
via semantic search to help the LLM fix similar problems in the future.

This creates a self-improving feedback loop:
  Error → logged → next time a similar error occurs → the fix is suggested.
"""

import datetime
import json
from pathlib import Path

import faiss
import numpy as np

from eda_agent.config import ERROR_LOG_PATH

# Lazy model loader (same as RAG module)
_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model


# ── Module-level singleton ────────────────────────────────────────────────

_error_kb = None


def get_error_kb() -> "ErrorKnowledgeBase | None":
    """Get or create the error knowledge base singleton."""
    global _error_kb
    if _error_kb is None and ERROR_LOG_PATH.exists():
        _error_kb = ErrorKnowledgeBase(str(ERROR_LOG_PATH))
    return _error_kb


def _invalidate():
    """Force reload on next access (call after writing to the log)."""
    global _error_kb
    _error_kb = None


def _rewrite_log(entries: list[dict]) -> None:
    """Overwrite the error log with the given entries list."""
    with open(ERROR_LOG_PATH, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


# ── Public helpers ────────────────────────────────────────────────────────

def log_error(task: str, attempt: int, error: str, code: str) -> None:
    """Append an error entry to the log.

    If a semantically similar error already exists (cosine >= 0.85),
    increment its frequency counter instead of creating a duplicate.
    """
    now = datetime.datetime.now().isoformat(timespec="seconds")

    err_kb = get_error_kb()
    if err_kb is not None:
        dup_idx = err_kb.find_duplicate(error, threshold=0.85)
        if dup_idx is not None:
            entry = err_kb.entries[dup_idx]
            entry["frequency"] = entry.get("frequency", 1) + 1
            entry["last_seen"] = now
            entry["resolved"] = False
            _rewrite_log(err_kb.entries)
            _invalidate()
            return

    new_entry = {
        "timestamp": now,
        "last_seen": now,
        "frequency": 1,
        "task": task,
        "attempt": attempt,
        "error": error,
        "code": code,
        "fix_code": None,
        "resolved": False,
    }
    with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(new_entry) + "\n")
    _invalidate()


def mark_resolved(task: str, fix_code: str, resolution_attempt: int) -> None:
    """Mark all unresolved log entries for this task as resolved."""
    _invalidate()
    err_kb = get_error_kb()
    if err_kb is None or not err_kb.entries:
        return

    changed = False
    for entry in err_kb.entries:
        if entry.get("task") == task and not entry.get("resolved", False):
            entry["resolved"] = True
            entry["fix_code"] = fix_code
            entry["resolution_attempt"] = resolution_attempt
            changed = True

    if changed:
        _rewrite_log(err_kb.entries)
        _invalidate()


# ── ErrorKnowledgeBase class ──────────────────────────────────────────────

class ErrorKnowledgeBase:
    """Semantic search over the agent error log.

    Capabilities:
      1. retrieve_similar_errors() — finds resolved past errors similar to a new
         error and returns formatted context for prompt injection.
      2. find_duplicate() — checks for near-identical existing errors (for dedup).
      3. get_frequency_summary() — returns top-N unresolved errors by frequency.
    """

    def __init__(self, log_path: str):
        self.log_path = Path(log_path)
        self.entries: list[dict] = []
        self.resolved_entries: list[dict] = []
        self.all_index: faiss.IndexFlatIP | None = None
        self.resolved_index: faiss.IndexFlatIP | None = None
        self._load_and_index()

    def retrieve_similar_errors(self, error_text: str, top_k: int = 3) -> str:
        """Find resolved past errors similar to the given error text.

        Returns a formatted block for prompt injection, or "" if none found.
        """
        if not self.resolved_entries or self.resolved_index is None:
            return ""

        model = _get_model()
        q_vec = model.encode([error_text], normalize_embeddings=True)
        n = min(top_k, len(self.resolved_entries))
        scores, indices = self.resolved_index.search(
            np.array(q_vec, dtype=np.float32), n
        )

        results = []
        for score, idx in zip(scores[0], indices[0]):
            if idx < 0 or score < 0.5:
                continue
            results.append((float(score), self.resolved_entries[idx]))

        if not results:
            return ""

        parts = ["[PAST ERROR RESOLUTIONS — from error log]"]
        for score, entry in results:
            err_preview = entry.get("error", "")[:500]
            parts.append(f"\n**Similar past error** (similarity: {score:.2f}):")
            parts.append(f"Error: {err_preview}")
            fix = entry.get("fix_code")
            if fix:
                parts.append(f"Fix code used:\n```python\n{fix}\n```")
            parts.append(f"Task context: {entry.get('task', 'N/A')}")
        parts.append("[END PAST RESOLUTIONS]")

        return "\n".join(parts)

    def find_duplicate(self, error_text: str, threshold: float = 0.85) -> int | None:
        """Return the index of a near-identical existing error, or None."""
        if not self.entries or self.all_index is None:
            return None

        model = _get_model()
        q_vec = model.encode([error_text], normalize_embeddings=True)
        scores, indices = self.all_index.search(
            np.array(q_vec, dtype=np.float32), 1
        )

        if indices[0][0] >= 0 and scores[0][0] >= threshold:
            return int(indices[0][0])
        return None

    def get_frequency_summary(self, top_n: int = 3) -> list[dict]:
        """Return top-N unresolved entries sorted by frequency (descending)."""
        unresolved = [e for e in self.entries if not e.get("resolved", False)]
        unresolved.sort(key=lambda e: e.get("frequency", 1), reverse=True)
        return unresolved[:top_n]

    # ── Internals ─────────────────────────────────────────────────────────

    def _load_and_index(self) -> None:
        """Load the JSONL log, build FAISS indices for all + resolved entries."""
        if not self.log_path.exists():
            return

        all_entries: list[dict] = []
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            all_entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except OSError:
            return

        if not all_entries:
            return

        self.entries = all_entries
        self.resolved_entries = [e for e in all_entries if e.get("resolved", False)]

        model = _get_model()

        # Index ALL entries (for duplicate detection)
        all_texts = [e.get("error", "")[:1000] for e in all_entries]
        all_emb = np.array(
            model.encode(all_texts, normalize_embeddings=True), dtype=np.float32
        )
        dim = all_emb.shape[1]
        self.all_index = faiss.IndexFlatIP(dim)
        self.all_index.add(all_emb)

        # Index only RESOLVED entries (for retrieval — don't poison with unsolved errors)
        if self.resolved_entries:
            res_texts = [e.get("error", "")[:1000] for e in self.resolved_entries]
            res_emb = np.array(
                model.encode(res_texts, normalize_embeddings=True), dtype=np.float32
            )
            self.resolved_index = faiss.IndexFlatIP(dim)
            self.resolved_index.add(res_emb)
