#!/bin/bash
set -euo pipefail

echo "🚀 Developer Farm Bootstrap (Ubuntu 22.04 | NVIDIA GPU | Python 3.11)"
echo "⚠️  All components will be installed to /opt/developer-farm"
echo "⚠️  /home is NOT used for models or virtual environments"
echo ""

# ─── Check privileges ──────────────────────────────────────────────────────
if [[ $EUID -eq 0 ]]; then
   echo "❌ Do not run as root. Use: sudo ./bootstrap.sh (sudo is handled inside)"
   exit 1
fi

PROJECT_DIR="/opt/developer-farm"
echo "📁 PROJECT_DIR=$PROJECT_DIR"

# ─── 1. System dependencies ────────────────────────────────────────────────
echo "📦 [1/7] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    git curl wget build-essential cmake \
    python3.11 python3.11-venv python3.11-dev python3-pip \
    redis-server sqlite3 libsqlite3-dev \
    nginx tmux htop nvtop jq

# ─── 2. Check NVIDIA drivers ──────────────────────────────────────────────
echo "🎮 [2/7] Checking NVIDIA drivers..."
if ! command -v nvidia-smi &> /dev/null; then
    echo "   Installing NVIDIA drivers (Pascal support)..."
    sudo apt-get install -y -qq nvidia-driver-535
    echo "   ⚠️  REBOOT REQUIRED after bootstrap!"
else
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)
    VRAM_TOTAL=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader | head -1)
    echo "   ✅ Drivers OK: $GPU_NAME ($VRAM_TOTAL MiB)"
fi

# ─── 3. Install Ollama (optimized for Pascal/4GB VRAM) ────────────────────
echo "🦙 [3/7] Installing Ollama & configuring for Pascal/4GB..."

# 1. Install Ollama (it starts automatically on default path)
if ! command -v ollama &> /dev/null; then
    curl -fsSL https://ollama.com/install.sh | sh
fi

# 2. Stop the service immediately to prevent mixed permission files
sudo systemctl stop ollama

# 3. Create directory for models
sudo mkdir -p "$PROJECT_DIR/models/ollama"

# 4. Configure systemd override FIRST
sudo mkdir -p /etc/systemd/system/ollama.service.d
cat << EOF | sudo tee /etc/systemd/system/ollama.service.d/gpu.conf > /dev/null
[Service]
Environment="OLLAMA_MODELS=$PROJECT_DIR/models/ollama"
Environment="OLLAMA_NUM_GPU=20"
Environment="OLLAMA_CONTEXT_LENGTH=4096"
Environment="OLLAMA_FLASH_ATTENTION=false"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
EOF

# 5. Fix permissions after paths are established
sudo chown -R ollama:ollama "$PROJECT_DIR/models/ollama"
sudo chmod -R 775 "$PROJECT_DIR/models/ollama"
sudo usermod -a -G ollama "$USER"

# 6. Reload daemon and FORCE a full restart of the service
sudo systemctl daemon-reload
sudo systemctl enable ollama
sudo systemctl restart ollama

sleep 5
echo "   ✅ Ollama configured & running"
echo "   ⚠️  Log out and back in for group changes to take effect"

# ─── 4. Python Virtual Environment ────────────────────────────────────────
echo "🐍 [4/7] Setting up Python 3.11 venv..."
cd "$PROJECT_DIR"
python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip setuptools wheel -q

echo "   📚 Installing Python packages..."
pip install -q \
    aiohttp aiohttp_sse aiohttp-sse-client \
    langgraph langchain-core langchain-openai langchain-community \
    pydantic redis httpx rich tenacity python-dotenv neo4j ast-grep-py tree-sitter tree-sitter-python

echo "   ✅ Python environment ready"

# ─── 5. Download model ───────────────────────────────────────────────────
echo "🧠 [5/7] Pulling qwen2.5-coder:3b-instruct (Q4_K_M)..."
echo "   ⏳ This may take 2-5 minutes depending on your internet"
ollama pull qwen2.5-coder:3b-instruct
echo "   ✅ Model downloaded to $PROJECT_DIR/models/ollama"

# ─── 6. Generate .env and project structure ──────────────────────────────
echo "🏗️  [6/7] Creating project structure & .env..."
mkdir -p "$PROJECT_DIR"/{graph,dashboard,scripts,worktrees,logs,data,docs/assets}

cat > "$PROJECT_DIR/.env" << EOF
# === Developer Farm Configuration ===
# GPU: NVIDIA (Pascal or newer) | RAM: 16GB+ | Disk: /dev/sda

# LLM (Ollama OpenAI-compatible API)
OPENAI_API_BASE=http://localhost:11434/v1
OPENAI_API_KEY=ollama
MODEL_NAME=qwen2.5-coder:3b-instruct
MAX_CONTEXT_LENGTH=4096

# OpenRouter API (for Planning and Verification)
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxx

# LangGraph & Orchestrator
MAX_PARALLEL_WORKERS=3
CHECKPOINT_DB=$PROJECT_DIR/data/checkpoints.db
LANGGRAPH_CHECKPOINT_TTL=86400

# Dashboard
DASHBOARD_PORT=8080
DASHBOARD_HOST=0.0.0.0

# Token budget
TOKEN_BUDGET_PER_FEATURE=500000
TOKEN_BUDGET_WARNING_PCT=80
EOF

echo "   ⚠️  IMPORTANT: Edit .env and add your OPENROUTER_API_KEY"
echo "   Get one free at: https://openrouter.ai/keys"

# ─── 7. Git initialization (if not already done) ─────────────────────────
echo "🔧 [7/7] Initializing git repository..."
if [ ! -d ".git" ]; then
    git init -b main
    git config user.email "farm@developer.local"
    git config user.name "Developer Farm"
    echo "# Developer Farm" > README.md
    git add README.md
    git commit -m "chore: initial commit"
    echo "   ✅ Git repo initialized"
else
    echo "   ✅ Git repo already exists"
fi

# ─── Final summary ───────────────────────────────────────────────────────
echo ""
echo "=============================================="
echo "✅ Bootstrap Complete!"
echo "=============================================="
echo ""
echo "📁 Project:    $PROJECT_DIR"
echo "🦙 Model:      Qwen2.5-Coder-3B-Instruct (stored on /dev/sda)"
echo "🖥️  VRAM:       ~2.4GB GPU + ~1.8GB RAM offload (safe for 4GB)"
echo "💾 RAM:         16GB (Redis + LangGraph + aiohttp overhead < 500MB)"
echo ""
echo "Next steps:"
echo "  1. cd $PROJECT_DIR && source venv/bin/activate"
echo "  2. nano .env  # Add your OPENROUTER_API_KEY"
echo "  3. python -m graph.graph work/mvp/user-spec.md  # Run pipeline"
echo "  4. python -m dashboard.server  # Start dashboard (optional)"
echo ""
echo "⚠️  If NVIDIA drivers were installed for the first time -> REBOOT NOW"
echo "=============================================="
