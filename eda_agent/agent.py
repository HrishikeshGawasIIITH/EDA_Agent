"""
agent.py — Core agent loop: user task → RAG → LLM → execute → retry.

Orchestrates the full cycle:
  1. User describes a task in plain English.
  2. Agent retrieves relevant KB context (RAG + error history).
  3. LLM generates Python code as JSON {plan, code}.
  4. Code executes in a sandboxed namespace with `v` and `client` available.
  5. On error: retry with enriched context (up to MAX_RETRIES).
  6. Resolved errors are logged for future self-improvement.
"""

import json
import re
import traceback

from eda_agent.config import MAX_RETRIES
from eda_agent.llm.cost import CostTracker
from eda_agent.errors.error_log import log_error, mark_resolved, get_error_kb


def clean_json(text: str) -> str:
    """Strip markdown fences and extract the JSON object from LLM output.

    Handles:
      - ```json ... ``` fences
      - Leading/trailing whitespace
      - JSON embedded among conversational text
    """
    text = text.strip()

    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)

    # If it doesn't start with '{', try to find the JSON object
    if not text.startswith("{"):
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            text = match.group()

    return text.strip()


def execute_code(code: str, v, client) -> str:
    """Execute LLM-generated Python code in a sandboxed namespace.

    The code has access to:
      - v: VirtuosoAPI instance
      - client: SafeClientProxy wrapping the raw VirtuosoClient
      - Standard library imports (json, time, math)
      - virtuoso_bridge schematic/layout helpers

    Returns:
        The string value of the `result` variable set by the code,
        or a descriptive success/error message.
    """
    # Build the execution namespace with all available tools
    namespace = {"v": v, "client": client, "result": None}

    # Inject commonly-needed imports into the namespace
    try:
        import json as _json
        import time as _time
        import math as _math
        namespace.update({"json": _json, "time": _time, "math": _math})
    except ImportError:
        pass

    # Inject virtuoso-bridge helpers (these are used in generated code)
    try:
        from virtuoso_bridge.virtuoso.schematic import (
            schematic_create_inst_by_master_name as inst,
            schematic_create_pin as pin,
            schematic_label_instance_term as label,
        )
        from virtuoso_bridge.virtuoso.schematic.params import set_instance_params
        namespace.update({
            "inst": inst, "pin": pin, "label": label,
            "set_instance_params": set_instance_params,
        })
    except ImportError:
        pass

    try:
        from virtuoso_bridge.virtuoso.layout import (
            layout_create_rect as rect,
            layout_create_via_by_name as via,
        )
        namespace.update({"rect": rect, "via": via})
    except ImportError:
        pass

    try:
        from virtuoso_bridge.virtuoso.schematic.reader import read_schematic
        namespace["read_schematic"] = read_schematic
    except ImportError:
        pass

    # Execute in a restricted namespace
    exec(code, namespace)

    result = namespace.get("result")
    if result is not None:
        return str(result)
    return "Code executed successfully (no explicit result set)."


def _build_error_context(error_text: str, kb=None, error_kb=None) -> str:
    """Assemble troubleshooting context from RAG KB and error history."""
    parts = []

    # Get relevant documentation from the main KB
    if kb:
        try:
            kb_context = kb.retrieve_for_error(error_text, top_k=2)
            if kb_context:
                parts.append(kb_context)
        except Exception:
            pass

    # Get similar resolved errors from the error log
    if error_kb:
        try:
            err_context = error_kb.retrieve_similar_errors(error_text, top_k=2)
            if err_context:
                parts.append(err_context)
        except Exception:
            pass

    return "\n\n".join(parts)


def run_agent_cycle(chat_session, v, client, user_input: str,
                    model_display: str, kb=None, design_kb=None) -> None:
    """Execute a full agent cycle: user task → code → execute → retry.

    Args:
        chat_session: An LLM session (GeminiSession or OpenAISession).
        v: VirtuosoAPI instance.
        client: SafeClientProxy wrapping VirtuosoClient.
        user_input: The user's natural-language task description.
        model_display: Display name of the model (for status messages).
        kb: Optional main KnowledgeBase for RAG retrieval.
        design_kb: Optional design KnowledgeBase for RAG retrieval.
    """
    cost = CostTracker(model=model_display)

    # Enrich the user prompt with RAG context
    rag_context = ""
    for label_name, knowledge_base in [("Main KB", kb), ("Design KB", design_kb)]:
        if knowledge_base:
            try:
                ctx = knowledge_base.retrieve(user_input, top_k=3)
                if ctx:
                    rag_context += f"\n{ctx}\n"
            except Exception:
                pass

    prompt = user_input
    if rag_context:
        prompt = f"{user_input}\n\n{rag_context}"

    print(f"\n  🤖 Thinking ({model_display})...", flush=True)
    response = chat_session.send_message(prompt)
    cost.add(response)

    for attempt in range(1, MAX_RETRIES + 2):  # 1-indexed, includes initial attempt
        raw_text = response.text

        # Try to parse JSON
        try:
            cleaned = clean_json(raw_text)
            data = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            # Not JSON — treat as plain-text response (e.g. explanation)
            print(f"\n  {raw_text}")
            break

        plan = data.get("plan", "")
        code = data.get("code", "")

        if plan:
            print(f"\n  📋 Plan: {plan}")

        if not code or code.strip() in ('""', "''", "pass"):
            # No code to execute — just show the plan
            if plan:
                print(f"\n  💬 {plan}")
            break

        # Execute the generated code
        print(f"\n  ⚙️  Executing (attempt {attempt})...", flush=True)
        try:
            result = execute_code(code, v, client)
            print(f"  ✅ Result: {result}")

            # Log resolution if this was a retry
            if attempt > 1:
                mark_resolved(user_input, code, attempt)

            # Send execution result back for LLM analysis
            feedback = f"[SYSTEM: Execution Result]\nSUCCESS: {result}"
            response = chat_session.send_message(feedback)
            cost.add(response)

            # Print the LLM's analysis
            final_text = response.text
            try:
                final_data = json.loads(clean_json(final_text))
                if final_data.get("code", "").strip() not in ("", '""', "''", "pass"):
                    # LLM generated follow-up code — execute it
                    continue
                if final_data.get("plan"):
                    print(f"\n  {final_data['plan']}")
            except (json.JSONDecodeError, ValueError):
                print(f"\n  {final_text}")
            break

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            tb = traceback.format_exc()
            print(f"  ❌ Error: {error_msg}")

            # Log the error
            log_error(user_input, attempt, error_msg, code)

            if attempt > MAX_RETRIES:
                print(f"\n  🛑 Max retries ({MAX_RETRIES}) reached. Giving up.")
                break

            # Build troubleshooting context for the retry
            error_kb = get_error_kb()
            troubleshooting = _build_error_context(error_msg, kb, error_kb)

            retry_prompt = (
                f"[SYSTEM: Execution Result]\n"
                f"ERROR on attempt {attempt}/{MAX_RETRIES}: {error_msg}\n\n"
                f"Traceback:\n{tb}\n"
                f"Code that failed:\n```python\n{code}\n```\n"
            )
            if troubleshooting:
                retry_prompt += (
                    f"\n[TROUBLESHOOTING CONTEXT]\n{troubleshooting}\n"
                    f"[END TROUBLESHOOTING]\n"
                )
            retry_prompt += (
                "\nFix the error and return a new JSON with "
                '"plan", "code", and "retry": true.'
            )

            print(f"\n  🔄 Retrying ({attempt}/{MAX_RETRIES})...")
            response = chat_session.send_message(retry_prompt)
            cost.add(response)

    # Print cost summary
    print(f"\n  📊 {cost.summary()}")
