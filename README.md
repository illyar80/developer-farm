# 🏭 AI Developer Farm

**Goodhart-proof AI coding pipeline with architectural isolation.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.4+-green.svg)](https://github.com/langchain-ai/langgraph)

Autonomous AI development pipeline that generates code from specifications with **architectural guarantees against metric gaming**. Built on LangGraph, runs locally on consumer hardware.

> **TL;DR:** AI agents that can't cheat the tests because they never see them.

## ✨ Key Metrics

- ⏱️ **26 seconds** per feature (planning → execution → verification)
- 💰 **$0.03** per feature (vs $0.40+ for SaaS tools)
- 🔒 **Zero metric leakage** between layers (TypedDict enforced isolation)
- 🏠 **Runs locally** on GTX 1050 Ti (4GB VRAM) + 16GB RAM

## 🎯 The Problem: Goodhart's Law in AI Coding

> "When a measure becomes a target, it ceases to be a good measure."

When AI agents see tests and acceptance criteria, they inevitably optimize code for **passing tests** rather than **solving the problem**. Commercial tools try to fight this with prompts and post-review, but prompts are disciplinary measures, not architectural guarantees. Agents are often smarter than their prompts.

**Developer Farm** makes metric gaming **physically impossible** through strict 4-layer isolation:

```text
PLANNING        →  TaskInput (NO criteria)
                   ↓
EXECUTION       →  CodeArtifact (NO author info)
                   ↓
VERIFICATION    →  Verdict (score + reason)
                   ↓
RETRY LOOP      →  Abstract Feedback (NO rubric revealed)
```

| Layer | Input | 🚫 Restricted From |
| :--- | :--- | :--- |
| **Planning** | User spec, codebase | Execution results, verdicts |
| **Execution** | Task description | **Acceptance criteria, tests, rubrics** |
| **Verification** | Git diff, rubric | **Worker ID, task description, author** |
| **Optimization** | Aggregated metrics | Artifact contents, raw logs |

## 🏗 Architecture

### Core Components
- **LangGraph**: State machine with SQLite persistence and streaming.
- **Ollama + Qwen2.5-Coder-3B**: Local execution layer (free, 10-14 tok/s).
- **OpenRouter API**: Planning (Qwen-Max) and Verification (Qwen-Turbo).
- **Git Worktrees**: Isolated branches per worker (`agent/{task_id}-{id}`).
- **Reconciler**: Kubernetes-style control loop for auto-recovery.

### Retry Loop with Abstract Feedback

When verification fails, the system generates abstract guidance without revealing the rubric:

> ❌ **Leaking:** "Add docstring to `is_palindrome()` — the rubric requires it."
>
> ✅ **Abstract:** "Code quality needs improvement. Consider adding documentation for public APIs."

## 📊 Benchmarks

### Task: Python Calculator Module
*Spec: `add`, `subtract`, `multiply`, `divide`, division by zero handling, type hints.*

| Metric | Developer Farm | SaaS Competitors |
| :--- | :--- | :--- |
| **Total Time** | 26.4s | 1–3 mins |
| **Total Cost** | **$0.030** | $0.40 – $10+ |
| **Iterations** | 1 (Pass) | 2–4 (Avg) |
| **Verification Score** | 0.97 / 1.0 | N/A (Opaque) |

## 🚀 Quick Start

### Prerequisites
- Ubuntu 22.04 (Linux recommended)
- Python 3.11+
- NVIDIA GPU with 4GB+ VRAM (GTX 1050 Ti tested)
- 16GB RAM
- [OpenRouter API Key](https://openrouter.ai/keys)

### Installation

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/developer-farm.git
cd developer-farm

# 2. Bootstrap (installs venv, Ollama, models, deps)
chmod +x bootstrap.sh
./bootstrap.sh

# 3. Setup Env
source venv/bin/activate
cp .env.example .env
nano .env  # Add your OPENROUTER_API_KEY
```

### Usage

**1. Write a spec:**
```bash
mkdir -p work/my-feature
cat > work/my-feature/user-spec.md << 'SPEC'
# Feature: JWT Auth
Implement login and token refresh with FastAPI.
Constraints: Python 3.11+, RS256, rate limiting.
SPEC
```

**2. Run the pipeline:**
```bash
python -m graph.graph work/my-feature/user-spec.md
```

**3. Review & Merge:**
```bash
# View generated branch
git diff master...agent/task-001-<artifact_id>

# Merge
git merge agent/task-001-<artifact_id> --no-ff
```

## 📁 Project Structure

```
developer-farm/
├── bootstrap.sh              # One-click setup
├── contracts.py              # TypedDict layer boundaries (Core Security)
├── graph/
│   ├── graph.py              # StateGraph orchestration
│   ├── nodes.py              # Layer wrappers
│   └── reconciler.py         # Auto-recovery loop
├── nodes/
│   ├── planning.py           # Spec → Task (API)
│   ├── execution.py          # Task → Code (Local Ollama)
│   └── verification.py       # Code → Verdict (API)
├── utils/
│   └── git_worktree.py       # Git isolation manager
└── dashboard/                # Real-time monitoring UI
```

## 📚 Documentation

- **[Architecture Deep Dive](docs/ARCHITECTURE.md)** — How isolation works
- **[Goodhart's Law](docs/GOODHART.md)** — Why this matters
- **[Contributing](CONTRIBUTING.md)** — How to help

## 🤝 Contributing

Contributions are welcome! We are looking for:
- Support for more LLM providers (Anthropic, OpenAI)
- Enhanced dashboard metrics
- Kubernetes deployment manifests

**⚠️ Important:** Any PR must strictly maintain the **4-layer isolation**. Violating isolation (e.g., passing tests to the execution agent) will be rejected.

## 📄 License

[MIT License](LICENSE) — Open Source & Free for Commercial Use.

---
**Built by engineers who refuse to delegate understanding.**
