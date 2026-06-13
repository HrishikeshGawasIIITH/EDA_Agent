"""
cli.py — Interactive REPL entry point for EDA Agent.

Run:
    python -m eda_agent.cli

Commands:
    status          — Show current cellview (lib/cell/view)
    libs            — List all open Virtuoso libraries
    cells <lib>     — List cells in a library
    /rag <query>    — Semantic search on the knowledge base
    /scan           — Show scan summary (if library_scan_dataset.json exists)
    exit / quit / q — Exit
"""

import json
import os
import sys
from pathlib import Path

from virtuoso_bridge import VirtuosoClient

from eda_agent.config import KB_PATH, DATASET_PATH, LLM_PROVIDER
from eda_agent.virtuoso_api import VirtuosoAPI, SafeClientProxy
from eda_agent.prompts import build_system_prompt
from eda_agent.agent import run_agent_cycle


# ── Knowledge base loading (lazy singletons) ─────────────────────────────

_kb_cache: dict = {}


def _get_kb():
    """Lazy-load the main and design knowledge bases."""
    if "loaded" in _kb_cache:
        return _kb_cache.get("main"), _kb_cache.get("design")

    _kb_cache["loaded"] = True
    main_kb = design_kb = None

    if KB_PATH.exists():
        try:
            from eda_agent.rag import KnowledgeBase
            main_kb = KnowledgeBase(str(KB_PATH))
            _kb_cache["main"] = main_kb
            print(f"  📚 Main KB: {len(main_kb.sections)} sections indexed")
        except Exception as exc:
            print(f"  ⚠️  KB load failed: {exc}")

    # Design KB lives alongside the main KB
    design_kb_path = KB_PATH.parent / "design_knowledge_base.md"
    if design_kb_path.exists():
        try:
            from eda_agent.rag import KnowledgeBase
            design_kb = KnowledgeBase(str(design_kb_path))
            _kb_cache["design"] = design_kb
            print(f"  📐 Design KB: {len(design_kb.sections)} sections indexed")
        except Exception as exc:
            print(f"  ⚠️  Design KB load failed: {exc}")

    return main_kb, design_kb


# ── Design context for system prompt ─────────────────────────────────────

def _load_design_context() -> str:
    """Load compact design context from design_knowledge_base.md."""
    design_kb_path = KB_PATH.parent / "design_knowledge_base.md"
    if not design_kb_path.exists():
        return ""
    try:
        text = design_kb_path.read_text(encoding="utf-8")
        # Limit to 80KB to avoid ballooning the system prompt
        if len(text) > 80_000:
            text = text[:80_000] + "\n... (truncated)"
        return text
    except OSError:
        return ""


def _load_scan_summary() -> str:
    """Load a brief summary from the scan dataset."""
    if not DATASET_PATH.exists():
        return "No scan data found. Run: python -m tools.scan_libraries"
    try:
        with open(DATASET_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        libs = data.get("libraries", {})
        total_cells = sum(len(l.get("cells", {})) for l in libs.values())
        total_tb = sum(
            1 for l in libs.values()
            for c in l.get("cells", {}).values()
            if c.get("is_testbench")
        )
        lines = [
            f"Scan timestamp: {data.get('scan_timestamp', 'unknown')}",
            f"Libraries: {len(libs)}  |  Cells: {total_cells}  |  Testbenches: {total_tb}",
        ]
        for lib_name, lib_data in libs.items():
            cells = lib_data.get("cells", {})
            lines.append(f"  {lib_name}: {len(cells)} cells")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error reading scan data: {exc}"


# ── Chat session factory ─────────────────────────────────────────────────

def _build_chat_session(provider: str, system_prompt: str):
    """Create a chat session for the configured LLM provider.

    Returns:
        (session, model_display_name) tuple.
    """
    prov = provider.strip().lower()

    if prov in ("gemini_adc", "gemini", "google", "gemini_key"):
        from google import genai
        from google.genai import types
        from eda_agent.llm import GeminiSession

        api_key = os.environ.get("GOOGLE_API_KEY", "")
        if api_key:
            gc = genai.Client(api_key=api_key)
        else:
            gc = genai.Client(
                vertexai=True,
                project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
                location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
            )

        model = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")
        raw = gc.chats.create(
            model=model,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.1,
            ),
        )
        return GeminiSession(raw), f"gemini/{model}"

    elif prov in ("nvidia_nim", "nvidia", "nim", "deepseek_nim", "deepseek"):
        from openai import OpenAI
        from eda_agent.llm import OpenAISession

        api_key = os.environ.get("NVIDIA_API_KEY", "")
        if not api_key:
            raise ValueError(
                "NVIDIA_API_KEY not set — get one at https://build.nvidia.com"
            )

        default_model = (
            "deepseek-ai/deepseek-v4-pro"
            if prov in ("deepseek_nim", "deepseek")
            else "nvidia/nemotron-3-super-120b-a12b"
        )
        model = os.environ.get("NVIDIA_MODEL", default_model)
        oai = OpenAI(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key=api_key,
        )
        return OpenAISession(oai, model, system_prompt, temperature=0.1), f"nim/{model}"

    raise ValueError(
        f"Unknown LLM_PROVIDER='{provider}'. "
        "Use: gemini_adc | nvidia_nim | deepseek_nim"
    )


# ── Main REPL ─────────────────────────────────────────────────────────────

def main():
    """Interactive REPL: connect to Virtuoso, init LLM, and loop."""
    # Build system prompt with design context
    design_context = _load_design_context()
    system_prompt = build_system_prompt(design_context)
    if design_context:
        print(f"📐 Design KB loaded ({len(design_context) / 1024:.1f} KB)")

    # Connect LLM
    try:
        chat_session, model_display = _build_chat_session(LLM_PROVIDER, system_prompt)
    except (ValueError, ImportError) as exc:
        print(f"❌ LLM init failed: {exc}")
        return

    # Connect to Virtuoso
    print("Connecting to Virtuoso...")
    try:
        raw_client = VirtuosoClient.from_env()
        v = VirtuosoAPI(raw_client)
        client = SafeClientProxy(raw_client)
        t = raw_client.execute_skill("getCurrentTime()")
        print(f"✅ Connected  |  Server time: {t.output.strip()}")
    except Exception as exc:
        print(f"❌ Connection failed: {exc}")
        print("Run 'virtuoso-bridge start' and load the SKILL file in the CIW.")
        return

    # Pre-warm KB in background
    main_kb, design_kb = _get_kb()

    # REPL banner
    print("=" * 60)
    print(f"🤖 EDA Agent  ({model_display} | RAG + Design KB)")
    print("Commands: 'status', 'libs', 'cells <lib>', '/rag <query>', '/scan', 'exit'")
    print("=" * 60)

    while True:
        try:
            user_input = input("\n> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", "q"}:
            print("Goodbye!")
            break

        # ── Built-in commands ────────────────────────────────────────────

        if user_input.lower() == "status":
            try:
                raw = raw_client.execute_skill("_get_api_cv()")
                if not raw.output or "nil" in raw.output:
                    print("No cellview currently open.")
                else:
                    print(f"  Cell : {v.current_cell()}")
                    print(f"  Lib  : {v.current_library()}")
                    print(f"  View : {v.current_view()}")
            except Exception as exc:
                print(f"Status error: {exc}")
            continue

        if user_input.lower() == "libs":
            libs = v.list_libraries()
            print(f"  {len(libs)} libraries: {libs}")
            continue

        if user_input.lower().startswith("cells "):
            lib = user_input.split(None, 1)[1].strip()
            try:
                cells = v.list_cells(lib)
                print(f"  {len(cells)} cells in '{lib}': {cells}")
            except Exception as exc:
                print(f"  Error: {exc}")
            continue

        if user_input.lower().startswith("/rag "):
            query = user_input[5:].strip()
            if not query:
                print("  Usage: /rag <query>")
                continue
            for label, kb_inst in [("Main KB", main_kb), ("Design KB", design_kb)]:
                if kb_inst is None:
                    continue
                results = kb_inst.search(query, top_k=3)
                print(f"\n  [{label}] Top results for '{query}':")
                for score, sec in results:
                    preview = sec.content[:80].replace("\n", " ").strip()
                    print(f"  [{score:.3f}] {sec.path}")
                    print(f"          {preview}...")
            continue

        if user_input.lower() == "/scan":
            print(f"\n{_load_scan_summary()}")
            continue

        # ── Agent cycle ──────────────────────────────────────────────────

        run_agent_cycle(
            chat_session, v, client, user_input, model_display,
            kb=main_kb, design_kb=design_kb,
        )


if __name__ == "__main__":
    main()
