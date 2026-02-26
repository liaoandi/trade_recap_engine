#!/bin/bash
# Simple Wrapper for US Stocks Review Workflows

COMMAND=$1
FILE=$2
TODAY=$(date +%Y%m%d)

case "$COMMAND" in
    pre)
        echo "🚀 Starting Premarket Session for $TODAY..."
        python3 auto_scripts/gemini_chat_session.py --mode premarket
        ;;
    in)
        echo "📈 Starting Intraday Session for $TODAY..."
        python3 auto_scripts/gemini_chat_session.py --mode intraday
        ;;
    recap)
        echo "📝 Generating Auto Recap for $TODAY..."
        python3 auto_scripts/session_to_recap.py
        ;;
    semi)
        if [ -z "$FILE" ]; then
            echo "❌ Please provide the markdown file path."
            echo "Example: ./trade.sh semi input/raw/chat.md"
            exit 1
        fi
        echo "🔄 Running Semi-Auto Recap on $FILE..."
        python3 semi_auto_scripts/gemini_vertex_recap.py --input "$FILE"
        ;;
    *)
        echo "🛠️  US Stocks Review - Quick Start"
        echo "==================================="
        echo "Usage: ./trade.sh [command]"
        echo ""
        echo "Commands:"
        echo "  pre    - Start today's premarket chat (Auto Workflow)"
        echo "  in     - Start today's intraday chat (Auto Workflow)"
        echo "  recap  - Generate today's report (Auto Workflow)"
        echo "  semi   - Generate report from exported chat (Semi-Auto Workflow)"
        echo ""
        echo "Examples:"
        echo "  ./trade.sh pre"
        echo "  ./trade.sh recap"
        echo "  ./trade.sh semi input/raw/chat.md"
        ;;
esac
