# Gemini API Session Workflow (Additive)

This workflow is independent from existing scripts under `semi_auto_scripts/`.
It records discussions directly via Gemini API, so you do not need manual chat export for every step.

## 1) Premarket session

```bash
python3 auto_scripts/gemini_chat_session.py \
  --mode premarket \
  --date 20260219 \
  --session-id am01
```

- Log output: `auto_data/output/sessions/20260219/premarket_am01.jsonl`
- Use `/exit` to stop interactive mode.

Single-turn mode:

```bash
python3 auto_scripts/gemini_chat_session.py \
  --mode premarket \
  --date 20260219 \
  --session-id am01 \
  --message "APP盘前关键位和计划是什么？"
```

## 2) Intraday session

```bash
python3 auto_scripts/gemini_chat_session.py \
  --mode intraday \
  --date 20260219 \
  --session-id in01
```

- Log output: `auto_data/output/sessions/20260219/intraday_in01.jsonl`

## 3) Postmarket recap from structured logs

```bash
python3 auto_scripts/session_to_recap.py --date 20260219
```

- Report output: `reports/20260219_Review_Gemini3_API.md`
- Merged prompt snapshot:
  `auto_data/processed/artifacts/sessions/20260219_session_merged.md`

## Notes

- Default model is `gemini-3-pro-preview`.
- Auth uses same order as existing scripts:
  1) Vertex project+credentials
  2) `GEMINI_API_KEY`
- This workflow does not modify old recap files and can run in parallel with your existing process.
