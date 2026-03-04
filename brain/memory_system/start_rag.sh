#!/bin/bash
# Start the Robothor RAG Orchestrator stack.
#
# Usage:
#   ./start_rag.sh          Start all components
#   ./start_rag.sh status   Check component status
#   ./start_rag.sh stop     Stop all components

set -e
cd "$(dirname "$0")"

case "${1:-start}" in
    start)
        echo "=== Starting Robothor RAG Stack ==="

        # 1. SearXNG
        echo "[1/3] Starting SearXNG..."
        sudo docker compose -f docker-compose.searxng.yml up -d 2>&1 | tail -1

        # 2. Verify Ollama
        echo "[2/3] Checking Ollama..."
        if curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
            echo "  Ollama running"
            MODELS=$(curl -s http://localhost:11434/api/tags | python3 -c "import sys,json; [print(f'    - {m[\"name\"]} ({m[\"size\"]//1024//1024}MB)') for m in json.load(sys.stdin).get('models',[])]" 2>/dev/null)
            echo "$MODELS"
        else
            echo "  WARNING: Ollama not running. Start with: systemctl start ollama"
        fi

        # 3. Start orchestrator
        echo "[3/3] Starting RAG Orchestrator on port 9099..."
        source venv/bin/activate
        nohup uvicorn orchestrator:app --host 0.0.0.0 --port 9099 > orchestrator.log 2>&1 &
        echo $! > orchestrator.pid
        echo "  PID: $(cat orchestrator.pid)"
        sleep 2

        # Health check
        if curl -s http://localhost:9099/health > /dev/null 2>&1; then
            echo ""
            echo "=== Stack Running ==="
            curl -s http://localhost:9099/health | python3 -m json.tool
        else
            echo "  WARNING: Orchestrator may still be starting. Check: curl http://localhost:9099/health"
        fi
        ;;

    status)
        echo "=== Robothor RAG Stack Status ==="
        echo ""

        # Ollama
        echo "Ollama:"
        if curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
            echo "  Status: running"
            curl -s http://localhost:11434/api/tags | python3 -c "import sys,json; [print(f'  - {m[\"name\"]}') for m in json.load(sys.stdin).get('models',[])]" 2>/dev/null
        else
            echo "  Status: NOT running"
        fi

        # SearXNG
        echo ""
        echo "SearXNG:"
        if curl -s http://localhost:8888/ > /dev/null 2>&1; then
            echo "  Status: running (port 8888)"
        else
            echo "  Status: NOT running"
        fi

        # Orchestrator
        echo ""
        echo "Orchestrator:"
        if curl -s http://localhost:9099/health > /dev/null 2>&1; then
            echo "  Status: running (port 9099)"
            curl -s http://localhost:9099/health | python3 -m json.tool
        else
            echo "  Status: NOT running"
        fi
        ;;

    stop)
        echo "=== Stopping Robothor RAG Stack ==="

        # Stop orchestrator
        if [ -f orchestrator.pid ]; then
            PID=$(cat orchestrator.pid)
            if kill -0 "$PID" 2>/dev/null; then
                kill "$PID"
                echo "Stopped orchestrator (PID $PID)"
            fi
            rm -f orchestrator.pid
        fi

        # Stop SearXNG
        sudo docker compose -f docker-compose.searxng.yml down 2>&1 | tail -1
        echo "Stopped SearXNG"
        ;;

    *)
        echo "Usage: $0 {start|status|stop}"
        exit 1
        ;;
esac
