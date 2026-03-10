#!/usr/bin/env python3
"""
Generate postmarket recap from JSONL session logs produced by gemini_chat_session.py.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from google import genai


AUTO_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = AUTO_ROOT.parent
AUTO_DATA_ROOT = PROJECT_ROOT / "auto_data"
SESSIONS_ROOT = AUTO_DATA_ROOT / "output" / "sessions"
REPORTS_ROOT = PROJECT_ROOT / "reports"
PROCESSED_ROOT = AUTO_DATA_ROOT / "processed" / "artifacts" / "sessions"
ENV_PATH = Path(os.getenv("ENV_PATH", "~/.config/api-keys.env")).expanduser()
DEFAULT_MODEL = "gemini-3.1-pro-preview"
AUTH_ENV_KEYS = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_PROJECT",
    "GEMINI_API_KEY",
    "VERTEX_LOCATION",
)


class RequestTimeoutError(RuntimeError):
    """Raised when a model request exceeds timeout."""


@contextlib.contextmanager
def alarm_timeout(seconds: int):
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def _handler(signum, frame):  # noqa: ANN001, ARG001
        raise RequestTimeoutError(f"timeout after {seconds}s")

    prev_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev_handler)


def load_env(path: Path) -> Dict[str, str]:
    env: Dict[str, str] = {
        key: value for key in AUTH_ENV_KEYS if (value := os.environ.get(key))
    }
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def detect_project(creds_path: str) -> Optional[str]:
    p = Path(creds_path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("project_id")
    except Exception:
        return None


def make_client(env: Dict[str, str], location: str) -> Tuple[genai.Client, str]:
    creds = env.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    api_key = env.get("GEMINI_API_KEY", "")
    if creds:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds
    if api_key:
        os.environ["GEMINI_API_KEY"] = api_key

    project = env.get("GOOGLE_CLOUD_PROJECT") or env.get("GOOGLE_PROJECT") or detect_project(creds)
    if project:
        client = genai.Client(vertexai=True, project=project, location=location)
        return client, f"vertex:{project}/{location}"
    if api_key:
        client = genai.Client(api_key=api_key)
        return client, "api-key"
    raise RuntimeError("No Gemini auth found. Need project+creds or GEMINI_API_KEY.")


def read_jsonl(path: Path) -> List[Dict]:
    rows: List[Dict] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            rows.append(json.loads(s))
        except Exception:
            continue
    return rows


def collect_rows(date_yyyymmdd: str, mode: str) -> List[Dict]:
    day_dir = SESSIONS_ROOT / date_yyyymmdd
    if not day_dir.exists():
        return []
    rows: List[Dict] = []
    for p in sorted(day_dir.glob(f"{mode}_*.jsonl")):
        rows.extend(read_jsonl(p))
    rows.sort(key=lambda x: (x.get("timestamp", ""), x.get("session_id", ""), x.get("turn", 0)))
    return rows


def timeline_text(rows: List[Dict], title: str) -> str:
    lines: List[str] = [f"## {title}", ""]
    if not rows:
        lines.append("(无记录)")
        return "\n".join(lines)
    for r in rows:
        ts = r.get("timestamp", "")
        sid = r.get("session_id", "")
        turn = r.get("turn", "")
        u = (r.get("user_text") or "").strip()
        a = (r.get("assistant_text") or "").strip()
        lines.append(f"### {ts} | {sid} | turn {turn}")
        lines.append("")
        lines.append("User:")
        lines.append(u)
        lines.append("")
        lines.append("Assistant:")
        lines.append(a)
        lines.append("")
    return "\n".join(lines).strip()


def build_prompt(date_yyyymmdd: str, pre_rows: List[Dict], intra_rows: List[Dict]) -> str:
    pre_text = timeline_text(pre_rows, "Premarket Session")
    intra_text = timeline_text(intra_rows, "Intraday Session")
    return (
        f"你是交易复盘教练。请基于 {date_yyyymmdd} 的盘前与盘中会话日志，输出盘后复盘 Markdown。\n"
        "必须包含：\n"
        "1. 当日关键时间线\n"
        "2. 原计划 vs 实际执行偏差\n"
        "3. 关键错误与纠偏\n"
        "4. 次日盘前计划（价格、触发条件、仓位）\n"
        "5. 可执行清单（不少于8条，量化）\n"
        "要求：具体、避免空话、只基于输入内容。\n\n"
        "---\n"
        f"{pre_text}\n\n"
        "---\n"
        f"{intra_text}\n"
        "---"
    )


def generate_text(client: genai.Client, model: str, prompt: str, timeout_s: int) -> str:
    with alarm_timeout(max(1, timeout_s)):
        resp = client.models.generate_content(model=model, contents=prompt)
    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("empty response")
    return text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate postmarket recap from structured session logs.")
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--location", default="global")
    parser.add_argument("--request-timeout", type=int, default=240)
    parser.add_argument("--out", default=None, help="Output file name. Default: YYYYMMDD_Review_Gemini3_API.md")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    pre_rows = collect_rows(args.date, "premarket")
    intra_rows = collect_rows(args.date, "intraday")
    if not pre_rows and not intra_rows:
        raise SystemExit(f"No session logs found for {args.date} under {SESSIONS_ROOT / args.date}")

    prompt = build_prompt(args.date, pre_rows, intra_rows)
    PROCESSED_ROOT.mkdir(parents=True, exist_ok=True)
    merged_path = PROCESSED_ROOT / f"{args.date}_session_merged.md"
    merged_path.write_text(prompt + "\n", encoding="utf-8")

    env = load_env(ENV_PATH)
    client, auth_mode = make_client(env, args.location)
    body = generate_text(client, args.model, prompt, args.request_timeout)

    out_name = args.out or f"{args.date}_Review_Gemini3_API.md"
    out_path = Path(out_name)
    if not out_path.is_absolute():
        REPORTS_ROOT.mkdir(parents=True, exist_ok=True)
        out_path = REPORTS_ROOT / out_path

    header = (
        "# 美股操作复盘（Session Pipeline）\n\n"
        f"- Model: `{args.model}`\n"
        f"- Auth: `{auth_mode}`\n"
        f"- Date: `{args.date}`\n\n"
    )
    out_path.write_text(header + body + "\n", encoding="utf-8")
    print(f"WROTE: {out_path}")
    print(f"MERGED_INPUT: {merged_path}")


if __name__ == "__main__":
    main()
