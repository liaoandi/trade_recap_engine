#!/usr/bin/env python3
"""
Pipeline:
1) Compare two chat export ZIP files (old vs new).
2) Build diff artifacts.
3) Call Gemini for markdown recap and strategy code generation.

Outputs are isolated in runs/<timestamp>/ to avoid conflicts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from google import genai


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OLD_ZIP = PROJECT_ROOT / "input" / "zips" / "old_20260213.zip"
DEFAULT_ZIPS_ROOT = PROJECT_ROOT / "input" / "zips"
DEFAULT_ENV = Path(os.getenv("ENV_PATH", "~/.config/api-keys.env")).expanduser()
ROOT = Path(__file__).resolve().parent
SEMI_DATA_ROOT = PROJECT_ROOT / "semi_data"
RUNS_ROOT = SEMI_DATA_ROOT / "processed" / "runs"
FIXED_MODEL = "gemini-3.1-pro-preview"
AUTH_ENV_KEYS = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_PROJECT",
    "GEMINI_API_KEY",
    "VERTEX_LOCATION",
)

DATE_RE = re.compile(r"^\*\*Date\*\*:\s*(.+)$", re.M)
TURN_RE = re.compile(r"^## Turn (\d+)\s*$", re.M)
ZIP_DATE_TOKEN_RE = re.compile(r"(20\d{6})")


@dataclass
class Turn:
    index: int
    text: str


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
    if not creds_path:
        return None
    p = Path(creds_path)
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload.get("project_id")


def extract_zip(src_zip: Path, out_dir: Path) -> None:
    if not src_zip.exists():
        raise FileNotFoundError(f"ZIP not found: {src_zip}")
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src_zip, "r") as zf:
        target_root = out_dir.resolve()
        for member in zf.infolist():
            member_path = Path(member.filename)
            if member_path.is_absolute():
                raise ValueError(f"Unsafe ZIP entry: {member.filename}")
            resolved_target = (target_root / member_path).resolve()
            if target_root not in resolved_target.parents and resolved_target != target_root:
                raise ValueError(f"Unsafe ZIP entry: {member.filename}")
            if member.is_dir():
                resolved_target.mkdir(parents=True, exist_ok=True)
                continue
            resolved_target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, "r") as src, resolved_target.open("wb") as dst:
                dst.write(src.read())


def read_chat_text(extract_dir: Path) -> str:
    chat = extract_dir / "chat.md"
    if not chat.exists():
        raise FileNotFoundError(f"Missing chat.md under {extract_dir}")
    return chat.read_text(encoding="utf-8", errors="ignore")


def pick_latest_zip(zips_root: Path) -> Path:
    candidates = [p for p in zips_root.glob("*.zip") if p.is_file()]
    if not candidates:
        raise FileNotFoundError(f"No ZIP files found under {zips_root}")

    def _sort_key(p: Path) -> Tuple[int, float]:
        m = ZIP_DATE_TOKEN_RE.search(p.stem)
        date_score = int(m.group(1)) if m else 0
        return date_score, p.stat().st_mtime

    return max(candidates, key=_sort_key)


def get_date_label(chat_text: str) -> str:
    m = DATE_RE.search(chat_text)
    return m.group(1).strip() if m else "unknown"


def parse_turns(chat_text: str) -> List[Turn]:
    matches = list(TURN_RE.finditer(chat_text))
    turns: List[Turn] = []
    for i, m in enumerate(matches):
        idx = int(m.group(1))
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(chat_text)
        turns.append(Turn(index=idx, text=chat_text[start:end].strip()))
    return turns


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


def clipped(text: str, limit: int) -> str:
    body = text.strip()
    if len(body) <= limit:
        return body
    return body[:limit] + "\n...[truncated]..."


def unified_diff(old_text: str, new_text: str, context: int = 2) -> str:
    import difflib

    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile="old/chat.md",
            tofile="new/chat.md",
            n=context,
            lineterm="",
        )
    )


def build_diff_focus(
    old_turns: Sequence[Turn],
    new_turns: Sequence[Turn],
    *,
    changed_sample_limit: int = 16,
    excerpt_limit: int = 800,
) -> Tuple[str, List[int], List[int], List[int]]:
    old_map = {t.index: t for t in old_turns}
    new_map = {t.index: t for t in new_turns}

    old_nums = set(old_map)
    new_nums = set(new_map)
    common = sorted(old_nums & new_nums)
    added = sorted(new_nums - old_nums)
    removed = sorted(old_nums - new_nums)

    changed: List[int] = []
    for idx in common:
        if normalize_text(old_map[idx].text) != normalize_text(new_map[idx].text):
            changed.append(idx)

    lines: List[str] = []
    lines.append("# Diff Focus")
    lines.append("")
    lines.append(f"- changed_turns: {len(changed)}")
    lines.append(f"- added_turns: {len(added)}")
    lines.append(f"- removed_turns: {len(removed)}")
    lines.append("")

    if changed:
        lines.append("## Changed Turns (sample)")
        lines.append("")
        for idx in changed[:changed_sample_limit]:
            lines.append(f"### Turn {idx} (OLD)")
            lines.append("")
            lines.append(clipped(old_map[idx].text, excerpt_limit))
            lines.append("")
            lines.append(f"### Turn {idx} (NEW)")
            lines.append("")
            lines.append(clipped(new_map[idx].text, excerpt_limit))
            lines.append("")

    if added:
        lines.append("## Added Turns (full text)")
        lines.append("")
        for idx in added:
            lines.append(f"### Turn {idx}")
            lines.append("")
            lines.append(new_map[idx].text)
            lines.append("")

    return "\n".join(lines).rstrip() + "\n", changed, added, removed


def build_summary_prompt(
    old_date: str,
    new_date: str,
    changed: Sequence[int],
    added: Sequence[int],
    removed: Sequence[int],
    diff_focus: str,
) -> str:
    return textwrap.dedent(
        f"""
        你是一名交易复盘分析师。请只基于下面“对话差异内容”输出 Markdown 报告。

        背景：
        - 旧对话日期：{old_date}
        - 新对话日期：{new_date}
        - changed turns: {len(changed)}
        - added turns: {len(added)}
        - removed turns: {len(removed)}

        输出要求（Markdown）：
        1. 变化总览（3-6条）
        2. 关键策略变化（买入/卖出/仓位/风控/节奏）
        3. 关键错误与纠偏（尤其卖飞、追涨、情绪交易）
        4. 可执行规则清单（至少10条，必须量化）
        5. 明日执行模板（盘前/盘中/盘后）

        限制：
        - 必须明确区分“新增观点”和“被替换/被弱化观点”。
        - 不要写空话，不要引用外部信息，不要复述无关内容。

        下面是差异内容：
        ---
        {diff_focus}
        ---
        """
    ).strip()


def build_code_prompt(summary_md: str, diff_focus: str) -> str:
    return textwrap.dedent(
        f"""
        你是一名量化策略工程师。请根据下方“差异总结+对话差异内容”直接生成一个完整 Python 文件代码。

        代码要求：
        1. 文件名逻辑：strategy_from_diff.py（只输出代码，不要解释）
        2. 实现一个 StrategyEngine，至少包含：
           - evaluate_buy(...)
           - evaluate_sell(...)
           - position_sizing(...)
           - risk_guard(...)
        3. 规则必须体现：
           - 仅在日线收盘条件下触发卖出确认
           - 不亏损卖出优先（带可配置例外：灾难保护位）
           - 分批买入间距（默认 >= 15%）
           - Put Wall / Call Wall 作为买卖过滤器
           - 情绪交易抑制（冷静期或信号确认）
        4. 使用 dataclass，类型注解完整，包含 main() 演示。
        5. 代码必须可直接 `python3` 运行，不依赖第三方包。

        只输出一个 Python 代码块。

        参考：差异总结
        ---
        {summary_md}
        ---

        参考：对话差异内容
        ---
        {clipped(diff_focus, 22000)}
        ---
        """
    ).strip()


def extract_python_block(text: str) -> str:
    m = re.search(r"```python\s*(.*?)```", text, flags=re.S | re.I)
    if m:
        return m.group(1).strip() + "\n"
    return text.strip() + "\n"


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


def generate_text(client: genai.Client, model: str, prompt: str) -> Tuple[str, str]:
    try:
        resp = client.models.generate_content(model=model, contents=prompt)
        text = (resp.text or "").strip()
        if text:
            return text, model
        raise RuntimeError("empty response")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Gemini generation failed for model '{model}'. Last error: {e}") from e


def compile_check(py_file: Path) -> Tuple[bool, str]:
    import py_compile

    try:
        py_compile.compile(str(py_file), doraise=True)
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run zip diff + Gemini summary + strategy code pipeline.")
    parser.add_argument("--old-zip", type=Path, default=DEFAULT_OLD_ZIP)
    parser.add_argument(
        "--new-zip",
        type=Path,
        default=None,
        help="New ZIP path. If omitted, auto-pick latest ZIP under input/zips by date token/mtime.",
    )
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--location", default="global")
    parser.add_argument("--model", default=FIXED_MODEL)
    parser.add_argument("--runs-root", type=Path, default=RUNS_ROOT)
    args = parser.parse_args(argv)
    if (args.model or "").strip() != FIXED_MODEL:
        raise SystemExit(f"Only '{FIXED_MODEL}' is supported. Received: '{args.model}'.")
    new_zip = args.new_zip or pick_latest_zip(DEFAULT_ZIPS_ROOT)
    if args.new_zip is None:
        print(f"[auto] --new-zip not provided; using latest ZIP: {new_zip}")
    if args.old_zip.resolve() == new_zip.resolve():
        raise SystemExit(
            f"--old-zip and --new-zip resolve to the same file: {new_zip}. "
            "Please specify different inputs."
        )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = args.runs_root / f"zip_diff_{ts}"
    old_dir = run_dir / "old"
    new_dir = run_dir / "new"
    run_dir.mkdir(parents=True, exist_ok=True)

    extract_zip(args.old_zip, old_dir)
    extract_zip(new_zip, new_dir)

    old_chat = read_chat_text(old_dir)
    new_chat = read_chat_text(new_dir)
    old_date = get_date_label(old_chat)
    new_date = get_date_label(new_chat)
    old_turns = parse_turns(old_chat)
    new_turns = parse_turns(new_chat)

    udiff = unified_diff(old_chat, new_chat)
    write_text(run_dir / "chat_unified.diff", udiff)

    diff_focus, changed, added, removed = build_diff_focus(old_turns, new_turns)
    write_text(run_dir / "diff_focus.md", diff_focus)

    env = load_env(args.env)
    client, auth_mode = make_client(env, args.location)

    summary_prompt = build_summary_prompt(old_date, new_date, changed, added, removed, diff_focus)
    summary_md, summary_model = generate_text(client, FIXED_MODEL, summary_prompt)
    summary_out = run_dir / "gemini_diff_summary.md"
    write_text(
        summary_out,
        "# 美股操作对话差异总结（Gemini）\n\n"
        f"- auth: `{auth_mode}`\n"
        f"- model: `{summary_model}`\n"
        f"- old_date: `{old_date}`\n"
        f"- new_date: `{new_date}`\n"
        f"- changed_turns: `{len(changed)}`\n"
        f"- added_turns: `{len(added)}`\n"
        f"- removed_turns: `{len(removed)}`\n\n"
        + summary_md.strip()
        + "\n",
    )

    code_prompt = build_code_prompt(summary_md, diff_focus)
    code_raw, code_model = generate_text(client, FIXED_MODEL, code_prompt)
    code = extract_python_block(code_raw)
    code_out = run_dir / "gemini_strategy_from_diff.py"
    write_text(code_out, code)
    ok, compile_msg = compile_check(code_out)

    report = textwrap.dedent(
        f"""
        # Run Report

        - run_dir: `{run_dir}`
        - old_zip: `{args.old_zip}`
        - new_zip: `{new_zip}`
        - old_turns: `{len(old_turns)}`
        - new_turns: `{len(new_turns)}`
        - changed_turns: `{len(changed)}`
        - added_turns: `{len(added)}`
        - removed_turns: `{len(removed)}`
        - summary_model: `{summary_model}`
        - code_model: `{code_model}`
        - code_compile: `{"pass" if ok else "fail"}`
        - code_compile_detail: `{compile_msg}`

        ## Files
        - `chat_unified.diff`
        - `diff_focus.md`
        - `gemini_diff_summary.md`
        - `gemini_strategy_from_diff.py`
        """
    ).strip() + "\n"
    write_text(run_dir / "RUN_REPORT.md", report)

    print(f"RUN_DIR={run_dir}")
    print(f"SUMMARY={summary_out}")
    print(f"CODE={code_out}")
    print(f"CODE_COMPILE={'pass' if ok else 'fail'}")
    if not ok:
        print(f"CODE_COMPILE_DETAIL={compile_msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
