# Contributing to Developer Farm

Thank you for your interest in the project! We welcome contributions from the community.

## 🛠 Development Setup

1. **Clone & Bootstrap:**
   ```bash
   git clone https://github.com/YOUR_USERNAME/developer-farm.git
   cd developer-farm
   ./bootstrap.sh
   source venv/bin/activate
   ```

2. **Configure Environment:**
   - Copy `.env.example` to `.env` and add your OpenRouter API key.

3. **Run Tests:**
   - Run isolated node tests: `python -m nodes.execution`, `python -m nodes.planning`.
   - Run the full pipeline: `python -m graph.graph work/mvp/user-spec.md`.

## 🚨 Core Contribution Rules

**The most important rule in this project:**
**NEVER violate the 4-layer isolation.**

When submitting a Pull Request, ensure you adhere to these constraints:
- **Planning** must never see the result of Execution.
- **Execution** must **NEVER** see acceptance criteria, tests, or the verification rubric.
- **Verification** must **NEVER** see the worker ID or the original prompt context.

Any PR that attempts to "simplify" the system by leaking data between layers will be rejected. The integrity of the Goodhart-proof architecture is our primary value.

## 📝 Pull Request Process

1.  Fork the repository and create your branch from `main`.
2.  If you've changed the API, update the documentation.
3.  Ensure your code passes `contracts.py` validation (no leaked fields).
4.  Submit a Pull Request with a clear description of the changes.

## 🐛 Bug Reports

Please use the GitHub Issues tab with the `[BUG]` template. Include:
- OS, GPU, RAM specs.
- Python & Ollama versions.
- Full traceback logs.

## 💬 Community

- **Discussions:** For feature requests and architectural questions.
- **Issues:** For bugs and reproduction steps.

Let's build the most honest AI coding pipeline together! 🚀