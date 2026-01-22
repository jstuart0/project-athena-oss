#!/bin/bash
# =============================================================================
# Project Athena - Mac/Apple Silicon LLM Server Setup
# =============================================================================
#
# This script sets up Ollama and/or MLX-LM server on an Apple Silicon Mac
# for use as an LLM inference backend for Project Athena.
#
# Requirements:
#   - macOS on Apple Silicon (M1/M2/M3/M4)
#   - Internet connection for downloading packages and models
#
# Usage:
#   ./setup-mac-studio.sh [ollama|mlx|both]
#   Default: both
#
# After setup, services will be available at:
#   - Ollama:  http://<this-mac-ip>:11434 (Ollama API + OpenAI-compatible)
#   - MLX-LM:  http://<this-mac-ip>:8080  (OpenAI-compatible API)
#
# Both servers provide OpenAI-compatible APIs, so you can use either as
# the OLLAMA_URL in Project Athena's configuration.
#
# =============================================================================

set -e

MODE="${1:-both}"

# Detect local IP
get_local_ip() {
    # Try common interfaces
    for iface in en0 en1 en2 en3; do
        IP=$(ipconfig getifaddr "$iface" 2>/dev/null)
        if [ -n "$IP" ]; then
            echo "$IP"
            return
        fi
    done
    echo "localhost"
}

LOCAL_IP=$(get_local_ip)

echo "=========================================="
echo "Project Athena - LLM Server Setup"
echo "=========================================="
echo "Mode: $MODE"
echo "Detected IP: $LOCAL_IP"
echo "=========================================="
echo ""

# ============================================================================
# PREREQUISITES CHECK
# ============================================================================

# Check if running on macOS
if [[ "$(uname)" != "Darwin" ]]; then
    echo "ERROR: This script is designed for macOS"
    exit 1
fi

# Check for Apple Silicon
if [[ "$(uname -m)" != "arm64" ]]; then
    echo "ERROR: This script requires Apple Silicon (M1/M2/M3/M4)"
    echo "For Intel Macs or Linux, use Docker-based Ollama instead."
    exit 1
fi

echo "System: $(uname -m) macOS $(sw_vers -productVersion)"
echo ""

# ============================================================================
# HOMEBREW
# ============================================================================

install_homebrew() {
    if ! command -v brew &> /dev/null; then
        echo "Installing Homebrew..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

        # Add to PATH for Apple Silicon
        if [[ -f /opt/homebrew/bin/brew ]]; then
            echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
            eval "$(/opt/homebrew/bin/brew shellenv)"
        fi
    else
        echo "Homebrew: $(brew --version | head -1)"
    fi
}

# ============================================================================
# OLLAMA SETUP
# ============================================================================
setup_ollama() {
    echo ""
    echo "=========================================="
    echo "Setting up Ollama"
    echo "=========================================="

    # Install Ollama
    if ! command -v ollama &> /dev/null; then
        echo "Installing Ollama via Homebrew..."
        brew install ollama
    else
        echo "Ollama already installed: $(ollama --version 2>/dev/null || echo 'version unknown')"
    fi

    # Create launchd plist for Ollama to run as a service
    echo "Configuring Ollama as a background service..."
    echo "  - Listening on all interfaces (0.0.0.0:11434)"
    echo "  - Models kept in memory for 24h"

    OLLAMA_PLIST="$HOME/Library/LaunchAgents/com.ollama.server.plist"
    mkdir -p "$HOME/Library/LaunchAgents"

    cat > "$OLLAMA_PLIST" << 'PLIST_EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ollama.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/bin/ollama</string>
        <string>serve</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>OLLAMA_HOST</key>
        <string>0.0.0.0:11434</string>
        <key>OLLAMA_ORIGINS</key>
        <string>*</string>
        <key>OLLAMA_KEEP_ALIVE</key>
        <string>24h</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/ollama.out.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/ollama.err.log</string>
</dict>
</plist>
PLIST_EOF

    # Load the service
    launchctl unload "$OLLAMA_PLIST" 2>/dev/null || true
    launchctl load "$OLLAMA_PLIST"

    echo "Waiting for Ollama to start..."
    for i in {1..10}; do
        if curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
            echo "Ollama is running!"
            break
        fi
        sleep 1
    done

    if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
        echo "WARNING: Ollama may not be running. Check: tail -f /tmp/ollama.err.log"
        return 1
    fi

    # Pull recommended models
    echo ""
    echo "Pulling recommended LLM models..."
    echo "(This may take a while depending on your internet connection)"
    echo ""

    # Core models for Project Athena
    MODELS=(
        "qwen3:4b"       # Fast, good for most tasks
        "phi3:mini"      # Very fast, lightweight
        "llama3.2:3b"    # Good balance
    )

    for model in "${MODELS[@]}"; do
        echo "Pulling $model..."
        ollama pull "$model" || echo "  Warning: Failed to pull $model"
    done

    echo ""
    echo "Installed Ollama models:"
    ollama list
}

# ============================================================================
# MLX-LM SETUP
# ============================================================================
setup_mlx() {
    echo ""
    echo "=========================================="
    echo "Setting up MLX-LM Server"
    echo "=========================================="
    echo "MLX is Apple's machine learning framework optimized for Apple Silicon."
    echo "MLX-LM provides an OpenAI-compatible server for local inference."
    echo ""

    # Ensure Python 3.11+
    if ! command -v python3 &> /dev/null; then
        echo "Installing Python 3.11..."
        brew install python@3.11
    fi

    PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    echo "Python version: $PYTHON_VERSION"

    # Create dedicated virtual environment
    MLX_VENV="$HOME/.mlx-server"
    MLX_MODELS_DIR="$HOME/.mlx-models"

    if [ ! -d "$MLX_VENV" ]; then
        echo "Creating MLX virtual environment at $MLX_VENV..."
        python3 -m venv "$MLX_VENV"
    fi

    # Install packages
    echo "Installing MLX packages..."
    source "$MLX_VENV/bin/activate"
    pip install --upgrade pip -q
    pip install mlx mlx-lm huggingface_hub -q
    deactivate

    # Create models directory
    mkdir -p "$MLX_MODELS_DIR"

    # Download MLX-optimized models
    echo ""
    echo "Downloading MLX-optimized models..."
    echo "(These are quantized models optimized for Apple Silicon)"
    echo ""

    source "$MLX_VENV/bin/activate"

    # MLX Community models (4-bit quantized, efficient)
    MLX_MODELS=(
        "mlx-community/Qwen2.5-3B-Instruct-4bit:qwen2.5-3b-4bit"
        "mlx-community/Llama-3.2-3B-Instruct-4bit:llama-3.2-3b-4bit"
    )

    for model_spec in "${MLX_MODELS[@]}"; do
        IFS=':' read -r repo local_name <<< "$model_spec"
        local_path="$MLX_MODELS_DIR/$local_name"

        if [ -d "$local_path" ]; then
            echo "Model already exists: $local_name"
        else
            echo "Downloading $repo..."
            python3 -c "
from huggingface_hub import snapshot_download
try:
    snapshot_download('$repo', local_dir='$local_path')
    print('  Downloaded successfully')
except Exception as e:
    print(f'  Warning: {e}')
" || echo "  Warning: Failed to download $repo"
        fi
    done

    deactivate

    # Create MLX server startup script
    MLX_SERVER_SCRIPT="$MLX_VENV/start-server.sh"
    cat > "$MLX_SERVER_SCRIPT" << 'SCRIPT_EOF'
#!/bin/bash
# MLX-LM Server Startup Script
# This script is called by launchd to start the MLX-LM server

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/bin/activate"

# Configuration via environment variables
MODEL="${MLX_MODEL:-$HOME/.mlx-models/qwen2.5-3b-4bit}"
PORT="${MLX_PORT:-8080}"
HOST="${MLX_HOST:-0.0.0.0}"

echo "Starting MLX-LM server..."
echo "  Model: $MODEL"
echo "  Host: $HOST:$PORT"

# Start the OpenAI-compatible server
exec python3 -m mlx_lm.server --model "$MODEL" --host "$HOST" --port "$PORT"
SCRIPT_EOF
    chmod +x "$MLX_SERVER_SCRIPT"

    # Create launchd plist
    echo "Configuring MLX-LM as a background service..."

    MLX_PLIST="$HOME/Library/LaunchAgents/com.mlx.server.plist"

    cat > "$MLX_PLIST" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mlx.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>$MLX_SERVER_SCRIPT</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MLX_MODEL</key>
        <string>$MLX_MODELS_DIR/qwen2.5-3b-4bit</string>
        <key>MLX_PORT</key>
        <string>8080</string>
        <key>MLX_HOST</key>
        <string>0.0.0.0</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>HOME</key>
        <string>$HOME</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/mlx-server.out.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/mlx-server.err.log</string>
    <key>WorkingDirectory</key>
    <string>$MLX_MODELS_DIR</string>
</dict>
</plist>
PLIST_EOF

    # Load the service
    launchctl unload "$MLX_PLIST" 2>/dev/null || true
    launchctl load "$MLX_PLIST"

    echo "Waiting for MLX-LM server to start (first load may be slow)..."
    for i in {1..30}; do
        if curl -s http://localhost:8080/v1/models > /dev/null 2>&1; then
            echo "MLX-LM server is running!"
            break
        fi
        sleep 2
    done

    if ! curl -s http://localhost:8080/v1/models > /dev/null 2>&1; then
        echo "Note: MLX-LM server may still be loading. Check: tail -f /tmp/mlx-server.err.log"
    fi
}

# ============================================================================
# MAIN
# ============================================================================

install_homebrew

case $MODE in
    ollama)
        setup_ollama
        ;;
    mlx)
        setup_mlx
        ;;
    both|*)
        setup_ollama
        setup_mlx
        ;;
esac

# Final summary
echo ""
echo "=========================================="
echo "Setup Complete!"
echo "=========================================="
echo ""
echo "This machine's IP: $LOCAL_IP"
echo ""
echo "Available LLM Servers:"

if [[ "$MODE" == "ollama" || "$MODE" == "both" ]]; then
    echo ""
    echo "  OLLAMA"
    echo "  ------"
    echo "  API URL:     http://$LOCAL_IP:11434"
    echo "  OpenAI URL:  http://$LOCAL_IP:11434/v1"
    echo "  Test:        curl http://$LOCAL_IP:11434/api/tags"
    echo "  Logs:        tail -f /tmp/ollama.out.log /tmp/ollama.err.log"
    echo "  Manage:      ollama list | ollama pull <model> | ollama rm <model>"
fi

if [[ "$MODE" == "mlx" || "$MODE" == "both" ]]; then
    echo ""
    echo "  MLX-LM"
    echo "  ------"
    echo "  API URL:     http://$LOCAL_IP:8080"
    echo "  OpenAI URL:  http://$LOCAL_IP:8080/v1"
    echo "  Test:        curl http://$LOCAL_IP:8080/v1/models"
    echo "  Logs:        tail -f /tmp/mlx-server.out.log /tmp/mlx-server.err.log"
    echo "  Models dir:  ~/.mlx-models"
fi

echo ""
echo "=========================================="
echo "Project Athena Configuration"
echo "=========================================="
echo ""
echo "In your athena-prod ConfigMap, set OLLAMA_URL to one of:"
echo ""
echo "  # For Ollama:"
echo "  OLLAMA_URL: \"http://$LOCAL_IP:11434\""
echo ""
echo "  # For MLX-LM:"
echo "  OLLAMA_URL: \"http://$LOCAL_IP:8080/v1\""
echo ""
echo "  # For in-cluster Ollama (fallback):"
echo "  OLLAMA_URL: \"http://ollama:11434\""
echo ""
echo "=========================================="
