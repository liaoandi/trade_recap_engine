# 📈 US Stocks AI Review & Journaling

An AI-powered, terminal-based workflow for US stock market traders to log, analyze, and generate actionable post-market recaps using Google's Gemini models (via Vertex AI or API Keys).

This tool acts as your personal "Trading Coach", helping you combat FOMO, enforce trading rules, and structurally review your daily decisions.

## ✨ Features

- **Interactive Terminal Trading Log**: Chat directly in your terminal during Pre-market and Intraday sessions.
- **Multimodal Support**: Feed charts and screenshots directly into the terminal workflow.
- **Auto-Summarization Pipeline**: Automatically synthesizes hours of unstructured market chatter into a rigid, actionable Markdown report.
- **Semi-Auto Import**: Supports importing chat logs exported directly from the Gemini Web UI.
- **Privacy First**: Designed to easily split the codebase (public) from your actual trading data (private).

## 🚀 Quick Start

1. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environment:**
   Create a `.env` file in the project root with your credentials:
   ```env
   # Option A: Gemini API Key
   GEMINI_API_KEY="your_api_key_here"

   # Option B: Google Cloud Vertex AI (Recommended for Gemini 3)
   GOOGLE_APPLICATION_CREDENTIALS="/path/to/your/gcp-service-account.json"
   GOOGLE_CLOUD_PROJECT="your-project-id"
   VERTEX_LOCATION="us-central1"
   ```

3. **Run the Interactive Workflow:**
   We provide a simple wrapper script `trade.sh` to handle all daily operations.

   ```bash
   # 1. Start your premarket planning session
   ./trade.sh pre

   # 2. Log your intraday trades and thoughts
   ./trade.sh in

   # 3. Generate the final post-market recap
   ./trade.sh recap
   ```

## 📁 Repository Architecture

This repository contains only the **code/engine**. It is strictly recommended to keep your personal trading logs and reports out of this repository.

* `auto_scripts/` - Core Python logic for the interactive CLI & automated JSONL logging.
* `semi_auto_scripts/` - Legacy Python logic for importing and parsing Markdown exported from Web UIs.
* `trade.sh` - The main entrypoint for daily usage.
* `RUNBOOK.md` - Detailed internal instructions on how the scripts function.

---
*Disclaimer: This is a journaling and review tool. The AI models do not provide financial advice. Always trade at your own risk.*