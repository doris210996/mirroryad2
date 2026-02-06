#!/bin/bash
# Start both Flask and LLM services
# Usage: bash start.sh [--no-llm]

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

# Start LLM service in background (unless --no-llm)
if [[ "$1" != "--no-llm" ]]; then
    LLM_BIN="$DIR/llm-service/build/llm-service"
    MODEL="$DIR/llm-service/models/qwen2.5-1.5b-instruct-q4_k_m.gguf"

    if [[ -f "$LLM_BIN" && -f "$MODEL" ]]; then
        echo "[Start] Launching LLM service on port 8282..."
        "$LLM_BIN" --model "$MODEL" --port 8282 --threads 2 &
        LLM_PID=$!
        echo "[Start] LLM service PID: $LLM_PID"
        trap "kill $LLM_PID 2>/dev/null" EXIT
    else
        echo "[Start] LLM service not built or model not found — skipping"
        echo "        Build: cd $DIR/llm-service/build && cmake .. -DCMAKE_BUILD_TYPE=Release && make -j2"
        echo "        Model: download to $MODEL"
    fi
fi

# Start Flask server
echo "[Start] Launching Flask server on port 8181..."
cd "$DIR"
python3 server.py
