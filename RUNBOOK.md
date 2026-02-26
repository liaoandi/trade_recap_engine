# Workflow Runbook

This project supports two primary workflows for generating trading session recaps: the **New Interactive CLI Workflow** (recommended for daily use) and the **Legacy Semi-Auto Web Import Workflow**.

## 1. Interactive CLI Workflow (Default & Simplest)

Bypass the complex Python scripts and generate reports directly through conversation with Gemini CLI.

**Process:**
1. **Chat & Analyze:** Paste your trades, screenshots, or thoughts directly into the CLI chat. We will discuss your strategy, analyze decisions (DCA, FOMO, etc.), and outline your plan.
2. **Generate Recap:** When finished, simply tell me:
   - *"Generate my premarket recap"*
   - *"Save the aftermarket review"*
3. **Automatic Saving:** I will automatically format our discussion into the standard markdown structure (including key timelines, plan vs. execution, errors, and next day's plan) and save it directly to the `reports/` directory.

## Quick Start (The `trade.sh` Script)

We have wrapped all the complicated Python commands into a single `trade.sh` script in the root directory. This automatically handles today's date and all the correct file paths.

### Auto Workflow (Terminal Based)
This is the recommended workflow. It opens a chat directly in your terminal and saves logs as you go.

1. **Start Premarket Chat:**
   ```bash
   ./trade.sh pre
   ```
2. **Start Intraday Chat:**
   ```bash
   ./trade.sh in
   ```
3. **Generate Today's Recap:**
   ```bash
   ./trade.sh recap
   ```
   *This outputs to: `reports/YYYYMMDD_Review_Gemini3_API.md`*

### Semi-Auto Workflow (Web Chat Import)
Use this if you still prefer chatting in the Gemini Web UI and exporting the markdown.

1. Drop your exported markdown file anywhere (e.g., `input/raw/`).
2. Run the script pointing to that file:
   ```bash
   ./trade.sh semi input/raw/YOUR_CHAT_EXPORT.md
   ```
   *This outputs to: `reports/YYYYMMDD_Review_Gemini3.md`*

## Naming Preference
Use consistent report naming across all workflows:
- `YYYYMMDD_Premarket_Review_Gemini.md`
- `YYYYMMDD_Aftermarket_Review_Gemini.md`
