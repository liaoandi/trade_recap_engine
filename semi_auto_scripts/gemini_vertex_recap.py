#!/usr/bin/env python3
"""
Generate US stocks recap markdown via Gemini on Vertex AI.

Diff mode pipeline:
1) Read old/new chat exports.
2) Extract NEW raw turns.
3) Split raw NEW turns into N segments (default 3).
4) Send each raw segment directly to Gemini.
5) Synthesize segment outputs into one final report.
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


SCRIPT_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_ROOT.parent
INPUT_BASE_ROOT = PROJECT_ROOT / "input" / "base"
INPUT_LATEST_ROOT = PROJECT_ROOT / "input" / "latest"
SEMI_DATA_ROOT = PROJECT_ROOT / "semi_data"
OUTPUT_REPORTS_ROOT = PROJECT_ROOT / "reports"
PROCESSED_DIFF_ROOT = SEMI_DATA_ROOT / "processed" / "artifacts" / "diff"
ENV_PATH = Path(os.getenv("ENV_PATH", "~/.config/api-keys.env")).expanduser()
FIXED_MODEL = "gemini-3.1-pro-preview"
TURN_BLOCK_RE = re.compile(r"(^## Turn \d+.*?)(?=^## Turn \d+|\Z)", re.S | re.M)
AUTH_ENV_KEYS = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_PROJECT",
    "GEMINI_API_KEY",
    "VERTEX_LOCATION",
)


class RequestTimeoutError(RuntimeError):
    """Raised when a model request exceeds the configured timeout."""


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
    """
    Reuse the same auth order as existing zip_diff_gemini_pipeline.py:
    1) Vertex (project + credentials)
    2) API key
    """
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


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


def extract_new_turns(old_text: str, new_text: str) -> str:
    """
    Return incremental NEW content by comparing old/new turn blocks.
    This avoids brittle fixed marker matching.
    """
    old_blocks = split_turn_blocks(old_text)
    new_blocks = split_turn_blocks(new_text)

    if old_blocks and new_blocks:
        i = 0
        max_prefix = min(len(old_blocks), len(new_blocks))
        while i < max_prefix and normalize_text(old_blocks[i]) == normalize_text(new_blocks[i]):
            i += 1
        return "".join(new_blocks[i:]).strip()

    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    i = 0
    max_prefix = min(len(old_lines), len(new_lines))
    while i < max_prefix and normalize_text(old_lines[i]) == normalize_text(new_lines[i]):
        i += 1
    return "".join(new_lines[i:]).strip()


def extract_report_date_yyyymmdd(text: str) -> str:
    date_patterns = [
        re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})"),
        re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"),
    ]
    candidates: List[Tuple[int, str]] = []
    for patt in date_patterns:
        for m in patt.finditer(text):
            y, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                d = datetime(y, mm, dd).strftime("%Y%m%d")
            except ValueError:
                continue
            candidates.append((m.start(), d))
    if candidates:
        candidates.sort(key=lambda x: x[0])
        return candidates[-1][1]
    return datetime.now().strftime("%Y%m%d")


def split_turn_blocks(text: str) -> List[str]:
    return [m.group(1) for m in TURN_BLOCK_RE.finditer(text)]


def split_new_turns_raw(new_turns: str, segments: int) -> List[str]:
    blocks = split_turn_blocks(new_turns)
    seg_n = max(1, int(segments))
    if not blocks:
        body = new_turns.strip()
        if not body:
            return [""]
        step = max(1, len(body) // seg_n)
        return [body[i : i + step] for i in range(0, len(body), step)]

    chunk = (len(blocks) + seg_n - 1) // seg_n
    parts: List[str] = []
    for i in range(seg_n):
        start = i * chunk
        end = min(len(blocks), (i + 1) * chunk)
        if start >= len(blocks):
            break
        parts.append("".join(blocks[start:end]).strip())
    return parts if parts else [new_turns.strip()]


def build_diff_segment_prompt(old_tail: str, new_raw_segment: str, seg_index: int, seg_total: int) -> str:
    return f"""
你是一位严谨的交易教练兼策略工程师。
这是“增量对话原文”的第 {seg_index}/{seg_total} 段（未压缩原文）。

任务：
1. 仅基于本段原文，提取关键事实（价格、动作、仓位、规则变化、失误/纠偏）。
2. 输出“本段结构化摘要”，格式为：
   - 时间线事实
   - 策略变化
   - 关键错误与教训
   - 可执行规则（量化）
3. 不要编造本段没有的信息。

---
### 旧版结尾（用于上下文锚定）
{old_tail}

---
### 新增原文（第 {seg_index}/{seg_total} 段）
{new_raw_segment}
---
""".strip()


def build_synthesis_prompt(segment_reports: List[str]) -> str:
    merged = "\n\n---\n\n".join([f"## Segment Report {i + 1}\n\n{x}" for i, x in enumerate(segment_reports)])
    return f"""
你是一位交易教练与策略工程师。下面是同一批增量对话的分段总结结果，请合并为最终报告。

最终输出结构：
1. 时间线还原（含关键日期）
2. 核心策略变化
3. 关键失误复盘（重点盘中误判与纠偏）
4. 仓位变化
5. 交易规则更新（新增/修改/废弃）
6. 买入/卖出评分（0-100）
7. 下一步执行计划（可执行）
8. Python伪代码（买入/卖出/仓位/风险/期权墙）

要求：
- 去重并合并冲突
- 简体中文 Markdown
- 数值具体，避免空话

---
{merged}
---
""".strip()


def build_prompt(chat_text: str) -> str:
    return f"""
你是一个严谨的交易教练。请基于以下会话完整内容，输出一份“可执行、细节充分”的美股操作复盘报告。

硬性要求：
1. 必须围绕本次美股操作会话展开（可含 APP/GOOG 等），按“事实->问题->修正->执行清单”结构。
2. 不能空泛，必须给出具体数值、阈值、触发条件。
3. 必须符合用户风格：不亏损卖出、长期看好APP、希望降低情绪化加仓。
4. 给出至少8条“下一次可直接照做”的规则。
5. 附一页“每日2分钟检查表”（勾选式）。
6. 输出使用简体中文，Markdown格式。

以下是会话内容：
---
{chat_text}
---
""".strip()


def generate_with_timeout(client: genai.Client, model: str, prompt: str, timeout_s: int) -> str:
    with alarm_timeout(max(1, timeout_s)):
        resp = client.models.generate_content(model=model, contents=prompt)
    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("empty response")
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate recap markdown using Gemini on Vertex.")
    parser.add_argument("--model", default=FIXED_MODEL)
    parser.add_argument("--out", default=None, help="Output markdown path. Default: YYYYMMDD_Review_Gemini3.md")
    parser.add_argument("--input", default=None, help="Input markdown path for non-diff mode. Default: input/base/chat.md")
    parser.add_argument("--location", default=None, help="Vertex location, e.g. global/us-central1")
    parser.add_argument("--request-timeout", type=int, default=120)
    parser.add_argument("--diff", action="store_true", help="Compare old vs new chat export and summarize diff")
    parser.add_argument("--diff-segments", type=int, default=3, help="Raw NEW turns split count in diff mode")
    args = parser.parse_args()

    if (args.model or "").strip() != FIXED_MODEL:
        raise SystemExit(f"Only '{FIXED_MODEL}' is supported. Received: '{args.model}'.")

    env = load_env(ENV_PATH)
    location = args.location or env.get("VERTEX_LOCATION", "global")
    try:
        client, auth_mode = make_client(env, location)
    except Exception as e:
        raise SystemExit(str(e))

    if args.diff:
        old_path = INPUT_BASE_ROOT / "chat.md"
        new_path = INPUT_LATEST_ROOT / "chat.md"
        for p, label in [(old_path, "old"), (new_path, "new")]:
            if not p.exists():
                raise SystemExit(f"Missing {label} chat: {p}")

        old_text = old_path.read_text(encoding="utf-8", errors="ignore")
        new_text = new_path.read_text(encoding="utf-8", errors="ignore")
        report_date = extract_report_date_yyyymmdd(new_text)
        new_turns = extract_new_turns(old_text, new_text)
        old_tail = old_text[-6000:]

        parts = split_new_turns_raw(new_turns, args.diff_segments)
        print(f"Diff mode: old={old_path.name}, new={new_path.name}")
        print(f"  New turns raw chars: {len(new_turns):,}")
        print(f"  Segments: {len(parts)}")

        segment_reports: List[str] = []
        for i, part in enumerate(parts, start=1):
            part_path = PROCESSED_DIFF_ROOT / f"diff_raw_part{i}.md"
            part_path.parent.mkdir(parents=True, exist_ok=True)
            part_path.write_text(part + "\n", encoding="utf-8")
            seg_prompt = build_diff_segment_prompt(old_tail, part, i, len(parts))
            print(f"Try segment {i}/{len(parts)} prompt chars={len(seg_prompt):,}")
            try:
                seg_text = generate_with_timeout(client, FIXED_MODEL, seg_prompt, args.request_timeout)
            except RequestTimeoutError:
                raise SystemExit(
                    f"Gemini call failed for model ['{FIXED_MODEL}']. Last error: timeout after {args.request_timeout}s at segment {i}"
                )
            except Exception as e:
                raise SystemExit(f"Gemini call failed for model ['{FIXED_MODEL}']. Last error: {e}")
            segment_reports.append(seg_text)

        synth_prompt = build_synthesis_prompt(segment_reports)
        print(f"Try synthesis prompt chars={len(synth_prompt):,}")
        try:
            result_text = generate_with_timeout(client, FIXED_MODEL, synth_prompt, args.request_timeout)
        except RequestTimeoutError:
            raise SystemExit(
                f"Gemini call failed for model ['{FIXED_MODEL}']. Last error: timeout after {args.request_timeout}s at synthesis"
            )
        except Exception as e:
            raise SystemExit(f"Gemini call failed for model ['{FIXED_MODEL}']. Last error: {e}")
    else:
        chat_path = Path(args.input) if args.input else INPUT_BASE_ROOT / "chat.md"
        if not chat_path.exists():
            raise SystemExit(f"Missing input file: {chat_path}")
        chat_text = chat_path.read_text(encoding="utf-8", errors="ignore")
        report_date = extract_report_date_yyyymmdd(chat_text)
        prompt = build_prompt(chat_text)
        print(f"Try full prompt chars={len(prompt):,}")
        try:
            result_text = generate_with_timeout(client, FIXED_MODEL, prompt, args.request_timeout)
        except RequestTimeoutError:
            raise SystemExit(
                f"Gemini call failed for model ['{FIXED_MODEL}']. Last error: timeout after {args.request_timeout}s"
            )
        except Exception as e:
            raise SystemExit(f"Gemini call failed for model ['{FIXED_MODEL}']. Last error: {e}")

    out_name = args.out or f"{report_date}_Review_Gemini3.md"
    out_path = Path(out_name)
    if not out_path.is_absolute():
        OUTPUT_REPORTS_ROOT.mkdir(parents=True, exist_ok=True)
        out_path = OUTPUT_REPORTS_ROOT / out_path
    header = (
        "# 美股操作复盘（Gemini Vertex生成）\n\n"
        f"- Model: `{FIXED_MODEL}`\n"
        f"- Auth: `{auth_mode}`\n\n"
    )
    out_path.write_text(header + result_text + "\n", encoding="utf-8")
    print(f"WROTE: {out_path}")
    print(f"MODEL: {FIXED_MODEL}")


if __name__ == "__main__":
    main()
