#!/usr/bin/env python3
"""
Gemini API chat session runner with persistent JSONL logs.

This is an additive workflow and does not replace existing scripts.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import mimetypes
import os
import re
import signal
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from google import genai
from google.genai import types


AUTO_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = AUTO_ROOT.parent
AUTO_DATA_ROOT = PROJECT_ROOT / "auto_data"
OUTPUT_SESSIONS_ROOT = AUTO_DATA_ROOT / "output" / "sessions"
REPORTS_ROOT = PROJECT_ROOT / "reports"
DEFAULT_CHARTS_ROOT = PROJECT_ROOT / "input" / "references" / "charts"
ENV_PATH = Path(os.getenv("ENV_PATH", "~/.config/api-keys.env")).expanduser()
DEFAULT_MODEL = "gemini-3.1-pro-preview"
VALID_MODES = {"premarket", "intraday", "postmarket"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
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


def default_session_id(date_yyyymmdd: str, mode: str) -> str:
    return f"{date_yyyymmdd}_{mode}"


def session_path(date_yyyymmdd: str, mode: str, session_id: str) -> Path:
    return OUTPUT_SESSIONS_ROOT / date_yyyymmdd / f"{mode}_{session_id}.jsonl"


def read_jsonl_events(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    rows: List[Dict] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            rows.append(json.loads(s))
        except Exception:
            continue
    return rows


def read_jsonl(path: Path) -> List[Dict]:
    completed_by_turn: Dict[int, Dict] = {}
    for row in read_jsonl_events(path):
        if row.get("status", "ok") != "ok":
            continue
        turn = row.get("turn")
        if isinstance(turn, int):
            completed_by_turn[turn] = row
    return [completed_by_turn[turn] for turn in sorted(completed_by_turn)]


def next_turn_number(path: Path) -> int:
    max_turn = 0
    for row in read_jsonl_events(path):
        turn = row.get("turn")
        if isinstance(turn, int) and turn > max_turn:
            max_turn = turn
    return max_turn + 1


def append_jsonl(path: Path, row: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def usage_payload(resp) -> Dict[str, int]:  # noqa: ANN001
    usage: Dict[str, int] = {}
    um = getattr(resp, "usage_metadata", None)
    if um is None:
        return usage

    keys = [
        "prompt_token_count",
        "candidates_token_count",
        "total_token_count",
        "cached_content_token_count",
        "thoughts_token_count",
    ]
    for k in keys:
        v = getattr(um, k, None)
        if isinstance(v, int):
            usage[k] = v

    if not usage and isinstance(um, dict):
        for k in keys:
            v = um.get(k)
            if isinstance(v, int):
                usage[k] = v

    if not usage and hasattr(um, "model_dump"):
        try:
            d = um.model_dump()
            if isinstance(d, dict):
                for k in keys:
                    v = d.get(k)
                    if isinstance(v, int):
                        usage[k] = v
        except Exception:
            pass
    return usage


def extract_tags(user_text: str, assistant_text: str) -> List[str]:
    txt = f"{user_text}\n{assistant_text}"
    candidates = set(re.findall(r"\b[A-Z]{2,5}\b", txt))
    allowlist = {
        "APP",
        "GOOG",
        "NVDA",
        "PLTR",
        "TSLA",
        "AAPL",
        "MSFT",
        "META",
        "AMZN",
        "QQQ",
        "SPY",
        "TLT",
    }
    tags = sorted(x for x in candidates if x in allowlist)
    return tags


def system_prompt(mode: str) -> str:
    return (
        "你是美股交易助理。回答要具体、可执行、数字化。"
        f"当前会话模式是 `{mode}`。"
        "请在回答最后给出一个简短结构块，便于后续盘后复盘抽取：\n"
        "[PROTOCOL_BLOCK]\n"
        f"mode: {mode}\n"
        "focus: ...\n"
        "levels: ...\n"
        "actions: ...\n"
        "risk: ...\n"
        "[/PROTOCOL_BLOCK]"
    )


def build_prompt(mode: str, rows: List[Dict], user_text: str, max_history_turns: int) -> str:
    recent = rows[-max(0, max_history_turns) :]
    history_parts: List[str] = []
    for r in recent:
        turn = r.get("turn")
        u = (r.get("user_text") or "").strip()
        a = (r.get("assistant_text") or "").strip()
        history_parts.append(f"[Turn {turn}]\nUser: {u}\nAssistant: {a}")
    history_text = "\n\n".join(history_parts).strip()
    return (
        f"{system_prompt(mode)}\n\n"
        "外部文档上下文：\n"
        "(无)\n\n"
        "以下是最近会话记录（按时间顺序）：\n"
        f"{history_text if history_text else '(无历史)'}\n\n"
        "当前用户输入：\n"
        f"{user_text}\n\n"
        "请先给交易建议，再给 PROTOCOL_BLOCK。"
    )


def read_context_files(paths: Sequence[Path], max_chars: int) -> str:
    if not paths:
        return "(无)"
    parts: List[str] = []
    used = 0
    budget = max(1000, max_chars)
    for p in paths:
        if not p.exists() or not p.is_file():
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        remain = budget - used
        if remain <= 0:
            break
        chunk = txt[:remain]
        used += len(chunk)
        parts.append(f"### {p.name}\n{chunk}")
    return "\n\n".join(parts).strip() if parts else "(无)"


def dedupe_paths(paths: Sequence[Path]) -> List[Path]:
    out: List[Path] = []
    seen = set()
    for p in paths:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def auto_report_files(report_root: Path, max_files: int) -> List[Path]:
    if max_files <= 0 or not report_root.exists():
        return []
    cands = [
        p
        for p in report_root.glob("*_Review_*.md")
        if p.is_file()
    ]
    cands.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return cands[:max_files]


def auto_image_files(image_root: Path, max_files: int) -> List[Path]:
    if max_files <= 0 or not image_root.exists():
        return []
    cands = [p for p in image_root.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    cands.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return cands[:max_files]


def image_part(path: Path, max_bytes: int):
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise ValueError(f"image too large: {path} ({len(data)} bytes > {max_bytes})")
    mime, _ = mimetypes.guess_type(str(path))
    mime = mime or "image/png"
    return types.Part.from_bytes(data=data, mime_type=mime)


def build_prompt_with_context(
    mode: str,
    rows: List[Dict],
    user_text: str,
    max_history_turns: int,
    context_text: str,
) -> str:
    recent = rows[-max(0, max_history_turns) :]
    history_parts: List[str] = []
    for r in recent:
        turn = r.get("turn")
        u = (r.get("user_text") or "").strip()
        a = (r.get("assistant_text") or "").strip()
        history_parts.append(f"[Turn {turn}]\nUser: {u}\nAssistant: {a}")
    history_text = "\n\n".join(history_parts).strip()
    return (
        f"{system_prompt(mode)}\n\n"
        "外部文档上下文：\n"
        f"{context_text}\n\n"
        "以下是最近会话记录（按时间顺序）：\n"
        f"{history_text if history_text else '(无历史)'}\n\n"
        "当前用户输入：\n"
        f"{user_text}\n\n"
        "请先给交易建议，再给 PROTOCOL_BLOCK。"
    )


def generate_reply(client: genai.Client, model: str, prompt: str, timeout_s: int) -> Tuple[str, Dict[str, int]]:
    with alarm_timeout(max(1, timeout_s)):
        resp = client.models.generate_content(model=model, contents=prompt)
    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("empty response")
    return text, usage_payload(resp)


def generate_reply_multimodal(
    client: genai.Client,
    model: str,
    prompt: str,
    timeout_s: int,
    image_paths: Sequence[Path],
    image_max_bytes: int,
) -> Tuple[str, Dict[str, int]]:
    parts = [types.Part.from_text(text=prompt)]
    for p in image_paths:
        parts.append(image_part(p, image_max_bytes))
    contents = [types.Content(role="user", parts=parts)]
    with alarm_timeout(max(1, timeout_s)):
        resp = client.models.generate_content(model=model, contents=contents)
    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("empty response")
    return text, usage_payload(resp)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactive Gemini chat session with JSONL logs.")
    parser.add_argument("--mode", required=True, choices=sorted(VALID_MODES))
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"), help="Session date, e.g. 20260219")
    parser.add_argument("--session-id", default=None, help="Stable id for continuing a session")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--location", default="global")
    parser.add_argument("--request-timeout", type=int, default=180)
    parser.add_argument("--max-history-turns", type=int, default=30)
    parser.add_argument("--context-file", action="append", default=[], help="Extra markdown/text context file (repeatable)")
    parser.add_argument("--context-max-chars", type=int, default=50000, help="Total context chars budget across files")
    parser.add_argument("--auto-context", dest="auto_context", action="store_true", default=True, help="Auto include recent reports")
    parser.add_argument("--no-auto-context", dest="auto_context", action="store_false", help="Disable auto report context")
    parser.add_argument("--auto-report-count", type=int, default=3, help="How many recent reports to auto-include")
    parser.add_argument("--image-file", action="append", default=[], help="Image path for multimodal input (repeatable)")
    parser.add_argument("--auto-image", dest="auto_image", action="store_true", default=True, help="Auto include latest chart images")
    parser.add_argument("--no-auto-image", dest="auto_image", action="store_false", help="Disable auto image context")
    parser.add_argument("--auto-image-dir", default=str(DEFAULT_CHARTS_ROOT), help="Auto image directory")
    parser.add_argument("--auto-image-count", type=int, default=3, help="How many latest images to auto-include")
    parser.add_argument("--image-max-bytes", type=int, default=5_000_000, help="Per-image max bytes")
    parser.add_argument("--message", default=None, help="Single-turn mode if provided")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    session_id = args.session_id or default_session_id(args.date, args.mode)
    path = session_path(args.date, args.mode, session_id)
    rows = read_jsonl(path)
    manual_context_paths = [Path(x) for x in args.context_file]
    auto_context_paths = auto_report_files(REPORTS_ROOT, args.auto_report_count) if args.auto_context else []
    context_paths = dedupe_paths([*manual_context_paths, *auto_context_paths])
    context_text = read_context_files(context_paths, args.context_max_chars)
    manual_image_paths = [Path(x) for x in args.image_file]
    auto_image_paths = auto_image_files(Path(args.auto_image_dir), args.auto_image_count) if args.auto_image else []
    image_paths = dedupe_paths([*manual_image_paths, *auto_image_paths])

    env = load_env(ENV_PATH)
    client, auth_mode = make_client(env, args.location)

    print(f"SESSION_FILE: {path}")
    print(f"MODEL: {args.model}")
    print(f"AUTH: {auth_mode}")
    print(f"EXISTING_TURNS: {len(rows)}")
    if context_paths:
        print("CONTEXT_FILES:")
        for cp in context_paths:
            print(f"  - {cp}")
    if image_paths:
        print("IMAGE_FILES:")
        for ip in image_paths:
            print(f"  - {ip}")

    def run_turn(user_text: str) -> bool:
        nonlocal rows
        turn = next_turn_number(path)
        timestamp = datetime.now().isoformat(timespec="seconds")
        base_row = {
            "timestamp": timestamp,
            "date": args.date,
            "mode": args.mode,
            "session_id": session_id,
            "turn": turn,
            "model": args.model,
            "auth": auth_mode,
            "user_text": user_text,
            "assistant_text": "",
            "tags": [],
            "usage": {},
            "context_files": [str(x) for x in context_paths],
            "image_files": [str(x) for x in image_paths],
        }
        append_jsonl(path, {**base_row, "status": "pending"})
        prompt = build_prompt_with_context(
            args.mode,
            rows,
            user_text,
            args.max_history_turns,
            context_text,
        )
        try:
            if image_paths:
                assistant_text, usage = generate_reply_multimodal(
                    client,
                    args.model,
                    prompt,
                    args.request_timeout,
                    image_paths,
                    args.image_max_bytes,
                )
            else:
                assistant_text, usage = generate_reply(client, args.model, prompt, args.request_timeout)
        except Exception as exc:
            append_jsonl(
                path,
                {
                    **base_row,
                    "status": "failed",
                    "error": str(exc),
                },
            )
            print(f"\n[request failed] {exc}\n")
            return False
        row = {
            **base_row,
            "assistant_text": assistant_text,
            "tags": extract_tags(user_text, assistant_text),
            "usage": usage,
            "status": "ok",
        }
        append_jsonl(path, row)
        rows.append(row)
        print("\n--- Assistant ---")
        print(assistant_text)
        print("\n-----------------\n")
        return True

    if args.message:
        if not run_turn(args.message.strip()):
            raise SystemExit(1)
        return

    print("Interactive mode. Commands: /exit, /quit")
    while True:
        try:
            user_text = input("You> ").strip()
        except EOFError:
            print()
            break
        if not user_text:
            continue
        if user_text.lower() in {"/exit", "/quit"}:
            break
        run_turn(user_text)


if __name__ == "__main__":
    main()
