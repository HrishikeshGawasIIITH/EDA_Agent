# EDA Agent

An AI-powered assistant for **Cadence Virtuoso** IC design. It uses LLMs (Google Gemini / DeepSeek via NVIDIA NIM) with **RAG-based knowledge retrieval** to translate natural-language tasks into executable Python code that controls a live Virtuoso session.

Built on top of [virtuoso-bridge-lite](https://github.com/Arcadia-1/virtuoso-bridge-lite) for native connectivity to Cadence Virtuoso.

## Features

- **Natural language → circuit design** — Describe what you want in plain English, the agent generates and executes Python code
- **Multi-provider LLM support** — Google Gemini (API key or Vertex AI) and NVIDIA NIM (DeepSeek V4 Pro, Nemotron)
- **RAG knowledge retrieval** — Semantic search over the virtuoso-bridge-lite documentation using sentence-transformers + FAISS
- **Design knowledge base** — Scanned library data (transistor sizings, testbench patterns, CDF parameters) injected as context
- **Self-improving error handling** — Errors are logged with semantic dedup; resolved errors and their fixes are surfaced on future similar failures
- **Cost tracking** — Per-call token usage and cost estimation across all supported models
- **Library scanner** — Automated extraction of instance usage, transistor sizing, and Wp/Wn ratios from all custom Virtuoso libraries

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                      User (CLI REPL)                     │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐  │
│  │  RAG Engine  │   │  LLM Session │   │  Error KB    │  │
│  │ (FAISS +     │   │ (Gemini /    │   │ (semantic    │  │
│  │  sentence-   │   │  DeepSeek)   │   │  dedup +     │  │
│  │  transformers│   │              │   │  resolution) │  │
│  └──────┬───────┘   └──────┬───────┘   └──────┬───────┘  │
│         │                  │                  │          │
│         └──────────┬───────┘──────────────────┘          │
│                    │                                     │
│              ┌─────▼──────┐                              │
│              │ Agent Loop │ ◄── generate → execute →     │
│              │            │     retry with context       │
│              └─────┬──────┘                              │
│                    │                                     │
│              ┌─────▼──────┐                              │
│              │VirtuosoAPI │ ◄── Python methods wrapping  │
│              │            │     SKILL commands           │
│              └─────┬──────┘                              │
│                    │                                     │
├────────────────────┼─────────────────────────────────────┤
│              ┌─────▼──────┐                              │
│              │ virtuoso-  │ ◄── SSH tunnel + SKILL IPC   │
│              │ bridge-lite│                              │
│              └─────┬──────┘                              │
│                    │                                     │
│              ┌─────▼──────┐                              │
│              │  Cadence   │                              │
│              │  Virtuoso  │                              │
│              └────────────┘                              │
└──────────────────────────────────────────────────────────┘
```

## Prerequisites

- **Python 3.10+**
- **Cadence Virtuoso** with a running SKILL bridge daemon
- **virtuoso-bridge-lite** installed and configured (`virtuoso-bridge start`)
- An LLM API key (Google Gemini or NVIDIA NIM)

## Setup

```bash
# 1. Clone and enter the project
git clone <your-repo-url>
cd EDA_Agent_temp_2

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
cp .env.example .env
# Edit .env with your API keys and provider choice

# 5. Start the Virtuoso bridge (separate terminal)
virtuoso-bridge start

# 6. Run the agent
python -m eda_agent.cli
```

## Usage

### Interactive Session

```
$ python -m eda_agent.cli
📐 Design KB loaded (45.2 KB)
Connecting to Virtuoso...
✅ Connected  |  Server time: Jun 13 12:30:00 2026
============================================================
🤖 EDA Agent  (nim/deepseek-ai/deepseek-v4-pro | RAG + Design KB)
Commands: 'status', 'libs', 'cells <lib>', '/rag <query>', '/scan', 'exit'
============================================================

> Create a CMOS inverter with Wp=480n and Wn=210n in library Agentic_AI

  📋 Plan: Create inverter schematic with PMOS and NMOS in Agentic_AI library...
  ⚙️  Executing (attempt 1)...
  ✅ Result: Inverter 'inv' created in Agentic_AI with symbol view

  📊 Tokens — in: 4,521  out: 892  |  ~$0.0000
```

### Built-in Commands

| Command | Description |
|---------|-------------|
| `status` | Show current cellview (lib/cell/view) |
| `libs` | List all open Virtuoso libraries |
| `cells <lib>` | List cells in a library |
| `/rag <query>` | Semantic search on the knowledge base |
| `/scan` | Show library scan summary |
| `exit` | Exit the agent |

### Library Scanner

```bash
# Scan all custom libraries and build the design KB
python tools/scan_libraries.py

# Build the compact design KB from scan data
python tools/build_design_kb.py
```

## Project Structure

```
EDA_Agent_temp_2/
├── README.md                         # This file
├── .gitignore                        # Python + project-specific ignores
├── .env.example                      # Template env file (no secrets)
├── requirements.txt                  # Dependencies
├── eda_agent/                        # Main Python package
│   ├── __init__.py                   # Package metadata
│   ├── config.py                     # Environment, paths, constants
│   ├── virtuoso_api.py               # Python → SKILL bridge wrapper
│   ├── agent.py                      # Core loop: RAG → LLM → execute → retry
│   ├── cli.py                        # Interactive REPL entry point
│   ├── llm/                          # LLM provider abstraction
│   │   ├── base.py                   # Shared LLMResponse type
│   │   ├── gemini.py                 # Google Gemini session
│   │   ├── openai_compat.py          # OpenAI-compatible (NVIDIA NIM / DeepSeek)
│   │   └── cost.py                   # Token usage + cost tracking
│   ├── rag/                          # Retrieval-Augmented Generation
│   │   └── knowledge_base.py         # FAISS semantic search over markdown
│   ├── errors/                       # Error logging & learning
│   │   └── error_log.py              # Persistent error KB with dedup
│   └── prompts/                      # System prompts
│       ├── api_reference.py          # Full API reference for the LLM
│       └── system_prompt.py          # System prompt builder
├── tools/                            # Standalone scripts
│   ├── scan_libraries.py             # Virtuoso library scanner
│   └── build_design_kb.py            # Design KB generator
└── data/                             # Runtime data (gitignored)
    ├── virtuoso_bridge_knowledge_base.md
    ├── design_knowledge_base.md
    └── library_scan_dataset.json
```

## Configuration

All configuration is via environment variables (loaded from `.env`):

| Variable | Description | Default |
|----------|-------------|---------|
| `LLM_PROVIDER` | LLM backend to use | `gemini_adc` |
| `GOOGLE_API_KEY` | Gemini API key | — |
| `GOOGLE_CLOUD_PROJECT` | Vertex AI project ID | — |
| `GEMINI_MODEL` | Gemini model name | `gemini-2.5-pro` |
| `NVIDIA_API_KEY` | NVIDIA NIM API key | — |
| `NVIDIA_MODEL` | NIM model name | `deepseek-ai/deepseek-v4-pro` |

**Supported `LLM_PROVIDER` values:**
- `gemini_adc` / `gemini` — Google Gemini via API key or Vertex AI
- `nvidia_nim` / `nim` — NVIDIA NIM endpoint
- `deepseek_nim` / `deepseek` — DeepSeek via NVIDIA NIM

## Tech Stack

- **LLM Providers**: Google Gemini, DeepSeek V4 Pro (via NVIDIA NIM)
- **RAG**: sentence-transformers (`all-MiniLM-L6-v2`) + FAISS
- **EDA Bridge**: virtuoso-bridge-lite (SKILL IPC over SSH tunnel)

## License

This project is for academic and research purposes.
