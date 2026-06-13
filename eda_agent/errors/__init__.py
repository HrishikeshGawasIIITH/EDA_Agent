"""Error logging and learning — tracks runtime errors and provides context for retries."""

from eda_agent.errors.error_log import ErrorKnowledgeBase, log_error, mark_resolved, get_error_kb

__all__ = ["ErrorKnowledgeBase", "log_error", "mark_resolved", "get_error_kb"]
