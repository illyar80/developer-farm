# 📊 Reproducible Benchmarks

> **Note:** All metrics in this file are generated from actual pipeline runs, not projections. Developer Farm prioritizes verifiable evidence over vanity claims. Anyone can independently reproduce these benchmarks by following the steps below.

## 🛠 Prerequisites & Setup

```bash
# 1. Clone repository
git clone https://github.com/illyar80/developer-farm.git
cd developer-farm

# 2. Bootstrap environment (installs venv, Ollama, models, Python deps)
chmod +x bootstrap.sh
./bootstrap.sh
source venv/bin/activate

# 3. Configure API keys
cp .env.example .env
# Edit .env: add your OPENROUTER_API_KEY (Neo4j/Bright Data are optional)
