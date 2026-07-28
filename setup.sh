#!/bin/bash
set -e

echo "📈 Taaveti UPT — Setup"
echo "======================"
echo ""

# Check Python and uv
python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' || { echo "❌ Python 3.11+ required"; exit 1; }
command -v uv > /dev/null 2>&1 || { echo "❌ uv is required. Install it with: python3 -m pip install --user uv"; exit 1; }

# Install dependencies
# --locked prevents setup from silently changing the validated dependency set.
echo "📦 Installing locked dependencies..."
uv sync --locked

# Check for API key
if [ ! -f .env ] || ! grep -q "DEEPSEEK_API_KEY\|GROQ_API_KEY" .env 2>/dev/null; then
    echo ""
    echo "⚠️  No API key found. Create .env with:"
    echo "   LLM_PROVIDER=deepseek"
    echo "   DEEPSEEK_API_KEY=your_key_here"
    echo ""
    echo "Or use Ollama (free, local):"
    echo "   LLM_PROVIDER=ollama"
    echo ""
    read -p "Continue without API key? [y/N] " resp
    if [ "$resp" != "y" ]; then exit 0; fi
fi

# Initialize database
echo ""
echo "🗄️  Initializing database..."
uv run python main.py --init

echo ""
echo "📊 Populating market data (this takes ~3-5 minutes)..."
uv run python main.py --warmup

echo ""
echo "✅ Setup complete!"
echo ""
echo "Launch: uv run python server.py"
echo "Then open http://localhost:8080"
