#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."
source venv/bin/activate
source .env

echo "🔍 Checking Ollama status..."
if ! systemctl is-active --quiet ollama; then
    echo "⚠️  Ollama service is not running. Starting..."
    sudo systemctl start ollama
    sleep 3
fi

PROJECT_DIR="/opt/developer-farm"

echo "📊 Ollama config:"
echo "  VRAM layers:  ${OLLAMA_NUM_GPU:-20}"
echo "  Context len:  ${OLLAMA_CONTEXT_LENGTH:-4096}"
echo "  Flash Attn:   disabled (Pascal)"
echo "  Models path:  ${OLLAMA_MODELS:-$PROJECT_DIR/models/ollama}"
echo ""

# Проверка наличия модели
if ! ollama list | grep -q "qwen2.5-coder:3b-instruct"; then
    echo "⚠️  Model not found. Pulling..."
    ollama pull qwen2.5-coder:3b-instruct
fi

echo "🌐 Testing OpenAI-compatible API..."
RESPONSE=$(curl -s http://localhost:11434/v1/models | jq -r '.data[].id' 2>/dev/null || echo "error")

if [[ "$RESPONSE" == *"qwen2.5-coder"* ]]; then
    echo "✅ API OK: $RESPONSE"
    echo "📝 LangChain config will use:"
    echo "   base_url=$OPENAI_API_BASE"
    echo "   api_key=$OPENAI_API_KEY"
    echo "   model=$MODEL_NAME"
else
    echo "❌ API check failed. Response: $RESPONSE"
    echo "💡 Check logs: journalctl -u ollama -f"
    exit 1
fi

echo ""
echo "🚀 Ollama is ready. To run in background:"
echo "  tmux new -s llm"
echo "  cd $PROJECT_DIR"
echo "  source venv/bin/activate && source .env"
echo "  # Ollama runs as systemd service automatically"
echo "  # Press Ctrl+B, then D to detach"
echo ""
echo "🔍 Monitor VRAM/RAM:"
echo "  nvtop  # or watch -n 2 'free -h && nvidia-smi --query-gpu=memory.used,memory.total --format=csv'"
