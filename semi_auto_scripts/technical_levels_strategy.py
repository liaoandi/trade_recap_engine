#!/usr/bin/env python3
"""
Technical-level strategy helper:
1) Trade review report based on executed fills.
2) Multi-key-level accumulation + right-side confirmation for similar chart patterns.

- Default fills are removed intentionally.
- In signal mode, OHLCV will be pulled from API automatically (default yfinance)
  unless --csv is provided explicitly.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple, Sequence, Dict, Any
from collections import defaultdict


try:
    import yfinance as yf  # type: ignore

    HAS_YFINANCE = True
except Exception:  # noqa: BLE001
    HAS_YFINANCE = False

try:
    import matplotlib
    import matplotlib.pyplot as plt

    HAS_MPL = True
except Exception:  # noqa: BLE001
    HAS_MPL = False


@dataclass
class Fill:
    side: str  # "buy" / "sell"
    price: float
    units: float
    date: Optional[str] = None


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class PutWall:
    expiration: str
    strike: float
    open_interest: int
    volume: int
    iv: Optional[float] = None
    side: str = "put"


@dataclass
class Position:
    lots: List[Tuple[float, float]] = field(default_factory=list)  # (entry_price, units)

    def buy(self, price: float, units: float) -> None:
        self.lots.append((price, units))

    def total_units(self) -> float:
        return sum(u for _, u in self.lots)

    def avg_cost(self) -> float:
        total_u = self.total_units()
        if total_u <= 0:
            return 0.0
        return sum(p * u for p, u in self.lots) / total_u

    @staticmethod
    def lot_breakeven_price(entry_price: float, fee_rate: float) -> float:
        # Strict no-loss line for one lot:
        # sell_price * (1-fee) >= entry_price * (1+fee)
        if fee_rate >= 1:
            return float("inf")
        return entry_price * (1 + fee_rate) / (1 - fee_rate)

    def breakeven_price(self, fee_rate: float) -> float:
        # Position-average no-loss line with buy+sell fee.
        avg = self.avg_cost()
        if avg <= 0:
            return 0.0
        return self.lot_breakeven_price(avg, fee_rate)

    def sell(self, sell_price: float, units: float, fee_rate: float, no_loss_only: bool = True) -> float:
        if units <= 0:
            return 0.0

        remaining = units
        sold = 0.0
        # Sell highest-cost lots first, but each sold lot must satisfy strict no-loss.
        self.lots.sort(key=lambda x: x[0], reverse=True)
        new_lots: List[Tuple[float, float]] = []
        for price, lot_units in self.lots:
            if no_loss_only and sell_price < self.lot_breakeven_price(price, fee_rate):
                new_lots.append((price, lot_units))
                continue
            if remaining <= 0:
                new_lots.append((price, lot_units))
                continue
            take = min(lot_units, remaining)
            lot_left = lot_units - take
            sold += take
            remaining -= take
            if lot_left > 0:
                new_lots.append((price, lot_left))
        self.lots = new_lots
        return sold


class DataSourceError(RuntimeError):
    """Raised when market data cannot be fetched."""


def _to_iso_date(value: Optional[str], label: str) -> str:
    if value is None:
        return ""
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception as e:
        raise DataSourceError(f"{label} 需为 YYYY-MM-DD 格式: {value}") from e


def _to_iso_datetime(value: Optional[str], label: str) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d")
    except Exception as e:
        raise DataSourceError(f"{label} 需为 YYYY-MM-DD 格式: {value}") from e


def _latest_buy_fill(fills: List[Fill]) -> Optional[Fill]:
    for item in reversed(fills):
        if item.side == "buy":
            return item
    return None


def detect_key_levels(
    bars: List[Bar],
    option_walls: Optional[List[PutWall]],
    swing_lookback: int = 60,
    front_low_buf_pct: float = 0.005,
    integer_guard_offset: float = 1.5,
) -> Dict[str, float]:
    if not bars:
        return {}

    def _window_low(window: int) -> float:
        chunk = bars[max(0, len(bars) - max(1, window)) :]
        return min(b.low for b in chunk) if chunk else bars[-1].low

    pre_low_20 = _window_low(20)
    pre_low_60 = _window_low(max(1, swing_lookback))
    pre_low_120 = _window_low(120)

    # 兼容原有逻辑：pre_low 仍取 60 日窗口（原策略默认窗口）
    pre_low = pre_low_60

    wall_strike_1 = 0.0
    wall_strike_2 = 0.0
    wall_strike_3 = 0.0
    wall_oi_1 = 0.0
    wall_oi_2 = 0.0
    wall_oi_3 = 0.0
    if option_walls:
        top_walls = sorted(option_walls, key=lambda w: w.open_interest, reverse=True)[:3]
        if len(top_walls) >= 1:
            wall_strike_1 = float(top_walls[0].strike)
            wall_oi_1 = float(top_walls[0].open_interest)
        if len(top_walls) >= 2:
            wall_strike_2 = float(top_walls[1].strike)
            wall_oi_2 = float(top_walls[1].open_interest)
        if len(top_walls) >= 3:
            wall_strike_3 = float(top_walls[2].strike)
            wall_oi_3 = float(top_walls[2].open_interest)

    wall_strike = wall_strike_1 if wall_strike_1 > 0 else None
    wall_oi = int(wall_oi_1)
    integer_anchor = round(wall_strike if wall_strike is not None else bars[-1].close)
    integer_guard = integer_anchor - integer_guard_offset

    pre_low_plus = pre_low * (1 + front_low_buf_pct)
    pre_low_plus_20 = pre_low_20 * (1 + front_low_buf_pct)
    pre_low_plus_60 = pre_low_60 * (1 + front_low_buf_pct)
    pre_low_plus_120 = pre_low_120 * (1 + front_low_buf_pct)
    return {
        "pre_low": pre_low,
        "pre_low_plus_0.5pct": pre_low_plus,
        "pre_low_20": pre_low_20,
        "pre_low_60": pre_low_60,
        "pre_low_120": pre_low_120,
        "pre_low_plus_0.5pct_20": pre_low_plus_20,
        "pre_low_plus_0.5pct_60": pre_low_plus_60,
        "pre_low_plus_0.5pct_120": pre_low_plus_120,
        "wall_strike": wall_strike if wall_strike is not None else 0.0,
        "wall_oi": float(wall_oi),
        "wall_strike_1": wall_strike_1,
        "wall_oi_1": wall_oi_1,
        "wall_strike_2": wall_strike_2,
        "wall_oi_2": wall_oi_2,
        "wall_strike_3": wall_strike_3,
        "wall_oi_3": wall_oi_3,
        "integer_anchor": integer_anchor,
        "integer_guard": integer_guard,
    }


def describe_key_levels(levels: Dict[str, float], latest_close: Optional[float] = None) -> List[str]:
    if not levels:
        return ["关键点位: 缺少历史数据"]

    lines = ["=== 关键点位（按策略） ==="]
    pre_low = levels.get("pre_low")
    pre_low_plus = levels.get("pre_low_plus_0.5pct")
    pre_low_20 = levels.get("pre_low_20")
    pre_low_60 = levels.get("pre_low_60")
    pre_low_120 = levels.get("pre_low_120")
    pre_low_plus_20 = levels.get("pre_low_plus_0.5pct_20")
    pre_low_plus_60 = levels.get("pre_low_plus_0.5pct_60")
    pre_low_plus_120 = levels.get("pre_low_plus_0.5pct_120")
    wall = levels.get("wall_strike")
    wall_oi = levels.get("wall_oi")
    wall_1 = levels.get("wall_strike_1")
    wall_1_oi = levels.get("wall_oi_1")
    wall_2 = levels.get("wall_strike_2")
    wall_2_oi = levels.get("wall_oi_2")
    wall_3 = levels.get("wall_strike_3")
    wall_3_oi = levels.get("wall_oi_3")
    integer_guard = levels.get("integer_guard")
    integer_anchor = levels.get("integer_anchor")

    if wall_1 is not None and wall_1 > 0:
        lines.append(f"- Put Wall1（主位）: {wall_1:.2f}（OI {wall_1_oi:.0f}）")
    elif wall is not None:
        lines.append(f"- Put Wall（主位）: {wall:.2f}（OI {wall_oi:.0f}）")
    if wall_2 is not None and wall_2 > 0:
        lines.append(f"- Put Wall2: {wall_2:.2f}（OI {wall_2_oi:.0f}）")
    if wall_3 is not None and wall_3 > 0:
        lines.append(f"- Put Wall3: {wall_3:.2f}（OI {wall_3_oi:.0f}）")

    if pre_low_20 is not None and pre_low_plus_20 is not None:
        lines.append(f"- 前低20: {pre_low_20:.2f} -> 挂单20(+0.5%): {pre_low_plus_20:.2f}")
    if pre_low_60 is not None and pre_low_plus_60 is not None:
        lines.append(f"- 前低60: {pre_low_60:.2f} -> 挂单60(+0.5%): {pre_low_plus_60:.2f}")
    if pre_low_120 is not None and pre_low_plus_120 is not None:
        lines.append(f"- 前低120: {pre_low_120:.2f} -> 挂单120(+0.5%): {pre_low_plus_120:.2f}")
    if pre_low is not None and pre_low_plus is not None:
        lines.append(f"- 兼容主前低(60): {pre_low:.2f} -> 挂单1: {pre_low_plus:.2f}")
    lines.append(f"- 挂单2（整数位-1.5）: {integer_anchor:.0f} 附近 -> {integer_guard:.2f}")
    if latest_close is not None:
        if pre_low_plus_20 is not None:
            lines.append(f"- 最新离挂单20距离: {latest_close - pre_low_plus_20:.2f}")
        if pre_low_plus_60 is not None:
            lines.append(f"- 最新离挂单60距离: {latest_close - pre_low_plus_60:.2f}")
        if pre_low_plus_120 is not None:
            lines.append(f"- 最新离挂单120距离: {latest_close - pre_low_plus_120:.2f}")
        if wall_1 is not None and wall_1 > 0:
            lines.append(f"- 最新离 PutWall1 距离: {latest_close - wall_1:.2f}")
        elif wall is not None:
            lines.append(f"- 最新离 PutWall距离: {latest_close - wall:.2f}")
    return lines


class MarketDataClient:
    def __init__(self, provider: str = "yfinance") -> None:
        self.provider = provider.lower()
        if self.provider == "yfinance":
            if not HAS_YFINANCE:
                raise DataSourceError("yfinance 未安装，安装后重试：pip install yfinance")
        elif self.provider == "stooq":
            pass
        else:
            raise DataSourceError(f"不支持的数据源: {self.provider}")

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        if not symbol:
            raise DataSourceError("未提供 symbol")
        return symbol.strip().upper()

    def fetch_ohlcv(
        self,
        symbol: str,
        days: int = 260,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Bar]:
        symbol = self._normalize_symbol(symbol)
        end = end_date or datetime.now().strftime("%Y-%m-%d")
        start = start_date or (datetime.now() - timedelta(days=max(30, days))).strftime("%Y-%m-%d")
        if start > end:
            raise DataSourceError(f"起始日期大于结束日期: {start} > {end}")

        if self.provider == "stooq":
            stooq_symbol = symbol.lower()
            if "." not in stooq_symbol:
                stooq_symbol = f"{stooq_symbol}.us"
            # stooq 日K线下载接口返回从新到旧，需本地排序。
            url = f"https://stooq.com/q/d/l/?s={stooq_symbol}&i=d"
            try:
                with urllib.request.urlopen(url, timeout=20) as resp:
                    raw = resp.read().decode("utf-8", errors="ignore")
            except Exception as e:  # noqa: BLE001
                raise DataSourceError(f"stooq 拉取 {symbol} 历史数据失败: {e}") from e

            rows: List[Bar] = []
            lines = raw.strip().splitlines()
            if not lines:
                raise DataSourceError(f"stooq 未返回 {symbol} 的历史数据")
            rdr = csv.DictReader(lines)
            for row in rdr:
                try:
                    dt = row.get("Date", "") or ""
                    if not dt:
                        continue
                    open_p = float(row.get("Open", "0"))
                    high_p = float(row.get("High", "0"))
                    low_p = float(row.get("Low", "0"))
                    close_p = float(row.get("Close", "0"))
                    vol_p = float(row.get("Volume", "0") or 0)
                except Exception:
                    # 过滤 N/D 或空值行
                    continue
                rows.append(
                    Bar(
                        date=dt,
                        open=open_p,
                        high=high_p,
                        low=low_p,
                        close=close_p,
                        volume=vol_p,
                    )
                )

            rows = [r for r in rows if r.date >= start and r.date <= end]
            rows.sort(key=lambda b: b.date)
            if not rows:
                raise DataSourceError(f"无法拉取 {symbol} 的历史数据（{start} 到 {end}）")
            return rows

        # yfinance 按时间段拉取。若期内返回少，属于正常波动（周末/交易日差异）。
        try:
            hist = yf.Ticker(symbol).history(start=start, end=end, interval="1d")
        except Exception as e:  # noqa: BLE001
            raise DataSourceError(f"yfinance 拉取 {symbol} 历史数据失败: {e}") from e

        if hist is None or getattr(hist, "empty", True):
            raise DataSourceError(f"无法拉取 {symbol} 的历史数据（{start} 到 {end}）")

        rows: List[Bar] = []
        for idx, row in hist.iterrows():
            rows.append(
                Bar(
                    date=idx.strftime("%Y-%m-%d"),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row["Volume"]),
                )
            )
        return rows

    def fetch_put_walls(
        self,
        symbol: str,
        max_expirations: int = 3,
        top_n: int = 1,
        spot_price: Optional[float] = None,
        min_strike_ratio: float = 0.6,
        max_strike_ratio: float = 1.5,
    ) -> List[PutWall]:
        if self.provider == "stooq":
            return []

        symbol = self._normalize_symbol(symbol)
        try:
            ticker = yf.Ticker(symbol)
            expirations = list(ticker.options or [])[:max_expirations]
        except Exception as e:  # noqa: BLE001
            raise DataSourceError(f"yfinance 拉取 {symbol} 期权链失败: {e}") from e

        if not expirations:
            return []

        walls: List[PutWall] = []
        for exp in expirations:
            try:
                chain = ticker.option_chain(exp)
            except Exception as e:  # noqa: BLE001
                continue
            puts = chain.puts
            if puts is None or getattr(puts, "empty", True):
                continue
            if "openInterest" not in puts.columns or "volume" not in puts.columns:
                continue
            if spot_price is not None and min_strike_ratio > 0 and max_strike_ratio > 0:
                if min_strike_ratio >= max_strike_ratio:
                    raise DataSourceError("option strike ratio 配置错误：min_ratio 需 < max_ratio")
                min_strike = spot_price * min_strike_ratio
                max_strike = spot_price * max_strike_ratio
                puts = puts[
                    (puts["strike"] >= min_strike) & (puts["strike"] <= max_strike)
                ]
            ranked = puts.sort_values("openInterest", ascending=False).head(top_n)
            for _, row in ranked.iterrows():
                strike = float(row.get("strike", 0.0) or 0.0)
                oi = int(row.get("openInterest", 0) or 0)
                vol = int(row.get("volume", 0) or 0)
                iv_raw = row.get("impliedVolatility")
                iv = float(iv_raw) if iv_raw is not None and not _is_nan(iv_raw) else None
                if oi <= 0 and vol <= 0:
                    continue
                if strike <= 0:
                    continue
                walls.append(
                    PutWall(
                        expiration=str(exp),
                        strike=strike,
                        open_interest=oi,
                        volume=vol,
                        iv=iv,
                    )
                )
        return sorted(walls, key=lambda item: item.open_interest, reverse=True)


def _is_nan(v: Any) -> bool:
    try:
        return isinstance(v, float) and (v != v)
    except Exception:
        return False


def mean(xs: List[float]) -> Optional[float]:
    if not xs:
        return None
    return sum(xs) / len(xs)


def rolling_mean(xs: List[float], window: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(xs)
    if window <= 0:
        return out
    for i in range(window - 1, len(xs)):
        out[i] = sum(xs[i - window + 1 : i + 1]) / window
    return out


def rolling_std(xs: List[float], window: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(xs)
    if window <= 1:
        return out
    for i in range(window - 1, len(xs)):
        chunk = xs[i - window + 1 : i + 1]
        m = sum(chunk) / window
        var = sum((x - m) ** 2 for x in chunk) / window
        out[i] = math.sqrt(var)
    return out


def rolling_max(xs: List[float], window: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(xs)
    for i in range(window - 1, len(xs)):
        out[i] = max(xs[i - window + 1 : i + 1])
    return out


def rsi(closes: List[float], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))

    avg_gain = mean(gains)
    avg_loss = mean(losses)
    if avg_gain is None or avg_loss is None:
        return out

    out[period] = 100 - 100 / (1 + avg_gain / avg_loss if avg_loss != 0 else float("inf"))
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = max(0.0, diff)
        loss = max(0.0, -diff)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else float("inf")
        out[i] = 100 - 100 / (1 + rs)
    return out


def parse_csv(path: str) -> List[Bar]:
    bars: List[Bar] = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"Date", "Open", "High", "Low", "Close", "Volume"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV missing columns: {sorted(missing)}")
        for row in reader:
            bars.append(
                Bar(
                    date=row["Date"],
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=float(row["Volume"]),
                )
            )
    bars.sort(key=lambda b: b.date)
    return bars


def parse_fill_csv(path: str) -> List[Fill]:
    path_obj = Path(path)
    if not path_obj.exists():
        raise FileNotFoundError(f"买入文件不存在: {path}")

    out: List[Fill] = []
    with path_obj.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = {k.lower(): k for k in (reader.fieldnames or [])}

        def _pick(*names: str) -> Optional[str]:
            for n in names:
                if n.lower() in fields:
                    return fields[n.lower()]
            return None

        price_k = _pick("price", "cost", "trade_price", "fill_price")
        unit_k = _pick("units", "qty", "quantity", "size")
        side_k = _pick("side", "action")
        date_k = _pick("date", "time", "datetime")
        if price_k is None or unit_k is None:
            raise ValueError(
                "CSV 必须至少包含 price 和 units（或同义列 cost, qty）列，" \
                "可选 side/date"
            )

        for row in reader:
            side = (row.get(side_k) or "buy").lower().strip() if side_k else "buy"
            if side not in {"buy", "sell"}:
                side = "buy"
            out.append(
                Fill(
                    side=side,
                    price=float(row[price_k]),
                    units=float(row[unit_k]),
                    date=row.get(date_k) if date_k else None,
                )
            )
    return out


def parse_fill_json(path: str) -> List[Fill]:
    path_obj = Path(path)
    if not path_obj.exists():
        raise FileNotFoundError(f"买入JSON不存在: {path}")

    with path_obj.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        records: Sequence[Dict[str, Any]] = data.get("fills", [])
    else:
        records = data

    if not isinstance(records, list):
        raise ValueError("fills JSON 必须是 list，或含有 fills 字段的对象")

    out: List[Fill] = []
    for row in records:
        if not isinstance(row, dict):
            raise ValueError("fills JSON 的每条记录必须是对象")
        side = str(row.get("side", "buy")).lower()
        if side not in {"buy", "sell"}:
            side = "buy"
        out.append(
            Fill(
                side=side,
                price=float(row["price"]),
                units=float(row["units"]),
                date=str(row.get("date")) if row.get("date") is not None else None,
            )
        )
    return out


def parse_fills(
    raw: Optional[str],
    csv_path: Optional[str] = None,
    json_path: Optional[str] = None,
) -> List[Fill]:
    if raw:
        out: List[Fill] = []
        # 规则: "530:1,480:1,450:1,364.5:0.5"
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            price_s, units_s = part.split(":")
            out.append(Fill(side="buy", price=float(price_s), units=float(units_s)))
        if not out:
            raise ValueError("--fills 解析后为空")
        return out

    if csv_path:
        out = parse_fill_csv(csv_path)
        if out:
            return out
        raise ValueError(f"--fills-csv 解析后没有买卖记录: {csv_path}")

    if json_path:
        out = parse_fill_json(json_path)
        if out:
            return out
        raise ValueError(f"--fills-json 解析后没有买卖记录: {json_path}")

    raise ValueError("缺少交易明细。请使用 --fills 或 --fills-csv/--fills-json 显式传入，不再使用写死参数")


def review_report(
    fills: List[Fill],
    min_spacing_pct: float = 0.15,
    current_price: Optional[float] = None,
    fee_rate: float = 0.003,
) -> str:
    buys = [f for f in fills if f.side == "buy"]
    if not buys:
        return "No buy fills found."

    pos = Position()
    for b in buys:
        pos.buy(b.price, b.units)

    lines: List[str] = []
    lines.append("=== 技术价位策略复盘详报 ===")
    lines.append(f"买入次数: {len(buys)}")
    lines.append(f"总仓位(单位): {pos.total_units():.2f}")
    lines.append(f"加权成本: {pos.avg_cost():.2f}")
    lines.append(f"无亏卖出线(含费率{fee_rate*100:.2f}%): {pos.breakeven_price(fee_rate):.2f}")
    if current_price is not None:
        pnl_amt = (current_price - pos.avg_cost()) * pos.total_units()
        pnl_pct = (current_price / pos.avg_cost() - 1) * 100 if pos.avg_cost() > 0 else 0.0
        lines.append(f"按现价{current_price:.2f}估算浮盈亏: {pnl_amt:.2f} ({pnl_pct:.2f}%)")
    lines.append("")
    lines.append("1) 节奏检查（加仓间距）")
    spacing_fail = 0
    for i in range(1, len(buys)):
        prev = buys[i - 1].price
        cur = buys[i].price
        spacing = abs(cur - prev) / prev
        tag = "OK" if spacing >= min_spacing_pct else "过密"
        if tag == "过密":
            spacing_fail += 1
        lines.append(f"- {prev:.2f} -> {cur:.2f}: 间距 {spacing*100:.1f}% [{tag}]")

    lines.append("")
    lines.append("2) 仓位分配检查（越跌越重）")
    notional = [b.price * b.units for b in buys]
    lines.append(f"- 每笔金额: {[round(x, 2) for x in notional]}")
    if len(notional) >= 2:
        ok = all(notional[i] <= notional[i - 1] for i in range(1, len(notional)))
        lines.append(f"- 越跌加仓力度: {'OK' if ok else '加仓金额出现回撤失败'}")

    lines.append("")
    lines.append("3) 交易执行与仓位风险")
    avg = pos.avg_cost()
    latest = buys[-1]
    first = buys[0]
    lines.append(f"- 最后一笔加仓: {latest.price:.2f} x {latest.units}")
    lines.append(f"- 首笔加仓: {first.price:.2f} x {first.units}")
    if avg > 0 and first.price > avg * 1.15:
        lines.append(f"- 首次加仓与均价偏差: {((first.price / avg - 1) * 100):.1f}%")

    lines.append("")
    lines.append("4) 下阶段执行计划")
    lines.append("- 规则A: 价格未较上次买点下跌>=15%，禁止加仓。")
    lines.append("- 规则B: 仅在右侧确认加仓(收盘重回MA5并突破前2日高点)。")
    lines.append("- 规则C: 新增仓位金额按0.25/0.5/0.75递增，不再等股数。")
    lines.append("- 规则D: 到达无亏损卖出线后，优先减交易仓0.5单位。")

    lines.append("")
    lines.append("5) 可执行硬规则")
    lines.append("- 加仓间距 >= 15%，不到位不出手。")
    lines.append("- 右侧确认后再加：收盘重回MA5且不破前低。")
    lines.append("- 单日最多1次操作。")
    lines.append("- 不亏损卖出：仅在价格 >= 成本线*(1+手续费) 时减交易仓。")
    return "\n".join(lines)


def describe_put_walls(walls: List[PutWall], max_lines: int = 12) -> List[str]:
    if not walls:
        return ["Option wall（Put OI）不可用（API未返回有效数据）"]

    lines = [f"期权分布（Put OI）共{len(walls)}档，按执行价聚合:"]
    if max_lines <= 0:
        return lines + ["- 无有效展示档位"]

    by_strike: Dict[float, Dict[str, object]] = {}
    for w in walls:
        bucket = by_strike.setdefault(
            w.strike,
            {"oi": 0, "vol": 0, "exps": [], "iv": 0.0},
        )
        bucket["oi"] = bucket["oi"] + w.open_interest  # type: ignore[operator]
        bucket["vol"] = bucket["vol"] + w.volume  # type: ignore[operator]
        bucket["exps"].append(w.expiration)  # type: ignore[arg-type]
        if w.iv is not None:
            bucket["iv"] = max(bucket["iv"], w.iv)  # type: ignore[operator]

    sorted_buckets = sorted(by_strike.items(), key=lambda kv: kv[1]["oi"], reverse=True)  # type: ignore[index]
    for strike, stat in sorted_buckets[:max_lines]:
        exps: list[str] = stat["exps"]  # type: ignore[assignment]
        iv = float(stat["iv"])  # type: ignore[arg-type]
        lines.append(
            f"- strike ${strike:.2f}: OI={stat['oi']}, vol={stat['vol']}, IV={iv:.2f}, exps={','.join(sorted(set(exps)))}"
        )
    return lines


def _parse_bar_dates(bars: List[Bar]) -> List[datetime]:
    out: List[datetime] = []
    for b in bars:
        try:
            out.append(datetime.strptime(b.date, "%Y-%m-%d"))
        except Exception:
            out.append(datetime.fromtimestamp(0))
    return out


def build_wall_summary(walls: List[PutWall], max_lines: int = 12) -> List[Tuple[float, int, int, Optional[float], List[str]]]:
    if not walls:
        return []

    merged: Dict[float, Dict[str, object]] = defaultdict(lambda: {"oi": 0, "vol": 0, "exps": [], "iv": 0.0})
    for w in walls:
        bucket = merged[w.strike]
        bucket["oi"] = bucket["oi"] + w.open_interest  # type: ignore[operator]
        bucket["vol"] = bucket["vol"] + w.volume  # type: ignore[operator]
        bucket["exps"].append(w.expiration)  # type: ignore[arg-type]
        if w.iv is not None:
            bucket["iv"] = max(bucket["iv"], w.iv)  # type: ignore[operator]

    pairs = sorted(merged.items(), key=lambda kv: kv[1]["oi"], reverse=True)  # type: ignore[index]
    rows: List[Tuple[float, int, int, Optional[float], List[str]]] = []
    for strike, stat in pairs[:max_lines]:
        rows.append(
            (
                float(strike),
                int(stat["oi"]),  # type: ignore[arg-type]
                int(stat["vol"]),  # type: ignore[arg-type]
                float(stat["iv"]),  # type: ignore[arg-type]
                sorted(set(stat["exps"])),  # type: ignore[arg-type]
            )
        )
    return rows


def render_signal_chart(
    bars: List[Bar],
    ma5: List[Optional[float]],
    ma20: List[Optional[float]],
    upper: List[Optional[float]],
    lower: List[Optional[float]],
    symbol: str = "APP",
    option_walls: Optional[List[PutWall]] = None,
    wall_top_n: int = 8,
    output_path: Optional[str] = None,
) -> str:
    if not HAS_MPL:
        raise RuntimeError("未安装 matplotlib，安装后可用: pip install matplotlib")
    if not bars:
        raise RuntimeError("没有可用K线，无法绘图")

    close_prices = [b.close for b in bars]
    x_dates = _parse_bar_dates(bars)
    x_vals = list(range(len(bars)))

    if output_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(Path.cwd() / f"{symbol.lower()}_analysis_{ts}.png")

    fig, (ax_price, ax_wall) = plt.subplots(2, 1, figsize=(14, 10), gridspec_kw={"height_ratios": [3, 1]})
    for idx, bar in enumerate(bars):
        color = "#2ca02c" if bar.close >= bar.open else "#d62728"
        ax_price.plot([idx, idx], [bar.low, bar.high], color=color, linewidth=0.8)
        ax_price.plot([idx, idx], [bar.open, bar.close], color=color, linewidth=3)

    ax_price.plot(x_vals, close_prices, label="Close", linewidth=1.4, color="#1f77b4")
    ma5_plot = [v for v in ma5 if v is not None]
    if ma5_plot:
        ax_price.plot([i for i, v in enumerate(ma5) if v is not None], [v for v in ma5 if v is not None], label="MA5", linewidth=1.2)
    ma20_plot = [v for v in ma20 if v is not None]
    if ma20_plot:
        ax_price.plot([i for i, v in enumerate(ma20) if v is not None], [v for v in ma20 if v is not None], label="MA20", linewidth=1.2)

    if any(v is not None for v in lower):
        ax_price.plot(x_vals, [v if v is not None else float("nan") for v in lower], label="BB-L", color="#9467bd", alpha=0.8, linewidth=1.0)
    if any(v is not None for v in upper):
        ax_price.plot(x_vals, [v if v is not None else float("nan") for v in upper], label="BB-U", color="#8c564b", alpha=0.8, linewidth=1.0)

    if len(bars) > 200:
        step = max(1, len(bars) // 12)
        idxs = list(range(0, len(bars), step))
        ax_price.set_xticks([i for i in idxs])
        ax_price.set_xticklabels([x_dates[i].strftime("%m-%d") for i in idxs], rotation=30)

    ax_price.set_title(f"{symbol} Option / Market Structure ({x_dates[0].strftime('%Y-%m-%d')} -> {x_dates[-1].strftime('%Y-%m-%d')})")
    ax_price.set_ylabel("Price")
    ax_price.grid(alpha=0.2)
    ax_price.legend(loc="upper left")

    if option_walls:
        wall_rows = build_wall_summary(option_walls, max_lines=wall_top_n)
        if wall_rows:
            strikes = [r[0] for r in wall_rows]
            oi_vals = [r[1] for r in wall_rows]
            ax_wall.barh(strikes, oi_vals, height=10, color="#1f77b4", alpha=0.7)
            ax_wall.set_title("Put OI (by strike)")
            ax_wall.set_xlabel("OI")
            ax_wall.set_ylabel("Strike")
            ax_wall.grid(alpha=0.2)
            for y, v in zip(strikes, oi_vals):
                ax_wall.text(v, y, f"{v}", va="center", ha="left", fontsize=8)
    else:
        ax_wall.set_title("Put OI (no data)")
        ax_wall.set_xlabel("OI")
        ax_wall.set_ylabel("Strike")

    fig.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)
    return output_path


def _fmt_opt(x: Optional[float]) -> str:
    if x is None:
        return "--"
    return f"{x:.2f}"


def _describe_candle(bar: Bar) -> str:
    rng = (bar.high - bar.low)
    body = abs(bar.close - bar.open)
    if rng <= 0:
        return "十字"
    body_ratio = body / rng
    if body_ratio < 0.08:
        return "十字/小实体"
    if bar.close > bar.open:
        return "阳线"
    return "阴线"


def describe_market_structure(
    bars: List[Bar],
    ma5: List[Optional[float]],
    ma20: List[Optional[float]],
    upper: List[Optional[float]],
    lower: List[Optional[float]],
) -> List[str]:
    if not bars:
        return ["市场结构: 无法获取K线"]

    lines: List[str] = []
    lines.append("市场结构（均线 / K线 / 布林）")
    lines.append("最近5根K线:")
    start = max(0, len(bars) - 5)
    for i in range(start, len(bars)):
        b = bars[i]
        lines.append(
            f"- {b.date} O:{b.open:.2f} H:{b.high:.2f} L:{b.low:.2f} C:{b.close:.2f} 成交量:{int(b.volume)} "
            f"形态:{_describe_candle(b)} MA5:{_fmt_opt(ma5[i])} MA20:{_fmt_opt(ma20[i])} "
            f"BB20:({_fmt_opt(lower[i])}, {_fmt_opt(ma20[i])}, {_fmt_opt(upper[i])})"
        )

    b = bars[-1]
    i = len(bars) - 1
    if ma5[i] is not None and ma20[i] is not None:
        if ma5[i] > ma20[i]:
            lines.append(f"均线状态: MA5({ma5[i]:.2f}) 上穿 MA20({ma20[i]:.2f}) -> 短线偏强")
        elif ma5[i] < ma20[i]:
            lines.append(f"均线状态: MA5({ma5[i]:.2f}) 下穿 MA20({ma20[i]:.2f}) -> 短线偏弱")
        else:
            lines.append(f"均线状态: MA5 与 MA20 接近")
    lines.append(f"最新收盘: {b.close:.2f}, 最新K线形态: {_describe_candle(b)}")
    return lines


def run_signal_engine(
    bars: List[Bar],
    initial_fills: List[Fill],
    fee_rate: float = 0.003,
    min_spacing_pct: float = 0.15,
    max_total_units: float = 4.5,
    no_loss_sell: bool = True,
    option_wall_top_n: int = 12,
    chart_output: Optional[str] = None,
    chart_symbol: str = "TICKER",
    emit_trades: bool = True,
    emit_snapshot: bool = True,
    option_walls: Optional[List[PutWall]] = None,
) -> List[str]:
    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    highs = [b.high for b in bars]

    ma5 = rolling_mean(closes, 5)
    ma20 = rolling_mean(closes, 20)
    vol5 = rolling_mean(volumes, 5)
    vol20 = rolling_mean(volumes, 20)
    std20 = rolling_std(closes, 20)
    hh120 = rolling_max(highs, 120)
    rsi14 = rsi(closes, 14)

    upper = [None] * len(closes)
    lower = [None] * len(closes)
    for i in range(len(closes)):
        if ma20[i] is not None and std20[i] is not None:
            upper[i] = ma20[i] + 2 * std20[i]
            lower[i] = ma20[i] - 2 * std20[i]

    pos = Position()
    dated_fills_by_date: Dict[str, List[Fill]] = defaultdict(list)
    undated_fills: List[Fill] = []
    for fill in initial_fills:
        if fill.date:
            dt = _to_iso_datetime(fill.date, "fills date")
            if dt is None:
                undated_fills.append(fill)
            else:
                dated_fills_by_date[dt.strftime("%Y-%m-%d")].append(fill)
        else:
            undated_fills.append(fill)

    # Backward compatibility: undated fills are treated as pre-existing position.
    for fill in undated_fills:
        if fill.side == "buy":
            pos.buy(fill.price, fill.units)
        elif fill.side == "sell":
            pos.sell(fill.price, fill.units, fee_rate=fee_rate, no_loss_only=no_loss_sell)

    last_buy = _latest_buy_fill(undated_fills)
    last_buy_price = last_buy.price if last_buy else None
    last_buy_date = _to_iso_datetime(last_buy.date, "fills date") if last_buy and last_buy.date else None
    sell_watch_active = False
    sell_watch_start: Optional[str] = None
    last_confirmed_sell_price: Optional[float] = None
    log: List[str] = []

    key_levels = detect_key_levels(
        bars=bars,
        option_walls=option_walls,
        swing_lookback=60,
        front_low_buf_pct=0.005,
    )
    if option_walls or key_levels:
        log.extend(describe_key_levels(key_levels, latest_close=closes[-1] if closes else None))

    # 期权墙仅用于辅助判断，不直接强制阻断交易：用于人工核对。
    if option_walls:
        log.extend(describe_put_walls(option_walls, max_lines=max(1, option_wall_top_n)))
        log.append("")

    bar_dates = _parse_bar_dates(bars)
    warmup_start = bar_dates[20] if len(bar_dates) > 20 else bar_dates[-1]

    # Apply dated fills before warmup window so initial state at first signal bar is correct.
    for d in sorted(dated_fills_by_date.keys()):
        if d >= warmup_start.strftime("%Y-%m-%d"):
            continue
        for fill in dated_fills_by_date[d]:
            if fill.side == "buy":
                pos.buy(fill.price, fill.units)
                last_buy_price = fill.price
                last_buy_date = _to_iso_datetime(fill.date, "fills date") if fill.date else None
            elif fill.side == "sell":
                pos.sell(fill.price, fill.units, fee_rate=fee_rate, no_loss_only=no_loss_sell)

    for i in range(20, len(bars)):
        b = bars[i]
        close = closes[i]
        bar_date = bar_dates[i]

        # Apply dated fills on this bar date first, then compute strategy actions.
        for fill in dated_fills_by_date.get(b.date, []):
            if fill.side == "buy":
                pos.buy(fill.price, fill.units)
                last_buy_price = fill.price
                last_buy_date = _to_iso_datetime(fill.date, "fills date") if fill.date else None
            elif fill.side == "sell":
                pos.sell(fill.price, fill.units, fee_rate=fee_rate, no_loss_only=no_loss_sell)

        can_calc = all(x is not None for x in (ma5[i], ma20[i], vol5[i], vol20[i], upper[i], lower[i], hh120[i]))
        if not can_calc:
            continue

        drawdown = (hh120[i] - close) / hh120[i] if hh120[i] and hh120[i] > 0 else 0.0
        spacing_ok = last_buy_price is None or close <= last_buy_price * (1 - min_spacing_pct)
        time_gap_ok = False
        if last_buy_date is not None:
            if (bar_date - last_buy_date).days >= 14 and last_buy is not None and close <= last_buy.price * 0.85:
                time_gap_ok = True

        wall_level = key_levels.get("wall_strike", 0.0) if key_levels else 0.0
        structure_ok = wall_level > 0 and close <= wall_level

        bleeding = drawdown >= 0.35
        touched_lower = any(
            lower[j] is not None and closes[j] < lower[j] for j in range(max(0, i - 2), i + 1)
        )
        vol_shrink = vol5[i] is not None and vol20[i] is not None and vol5[i] < vol20[i] * 0.85
        right_side = (
            i >= 2
            and close > max(highs[i - 1], highs[i - 2])
            and closes[i - 1] <= ma5[i - 1]
            and close > ma5[i]
        )

        # Gemini3.0 风格关键点触发（结构位 / 时间位）
        buy_units = 0.0
        if emit_trades and (structure_ok or time_gap_ok) and spacing_ok:
            if drawdown >= 0.55:
                buy_units = 0.75
            elif drawdown >= 0.45:
                buy_units = 0.50
            else:
                buy_units = 0.25
            if pos.total_units() + buy_units > max_total_units:
                buy_units = max(0.0, max_total_units - pos.total_units())
            if buy_units > 0:
                pos.buy(close, buy_units)
                last_buy_price = close
                last_buy = Fill(side="buy", price=close, units=buy_units)
                last_buy_date = bar_date
                log.append(
                    f"{b.date} BUY {buy_units:.2f} @ {close:.2f} | reason=structure-time trigger"
                )

        # No-loss staged reduction:
        # 1) first signal only arms sell watch.
        # 2) confirmed weakness then trim.
        # 3) continuation breakdown then second trim.
        breakeven = pos.breakeven_price(fee_rate)
        sell_cross = i >= 1 and closes[i - 1] >= ma5[i - 1] and close < ma5[i]
        overheat = rsi14[i] is not None and rsi14[i] > 72 and upper[i] is not None and close > upper[i]
        weakness_signal = sell_cross or overheat
        no_loss_ok = close >= breakeven
        if emit_trades and weakness_signal and no_loss_ok and pos.total_units() > 0 and not sell_watch_active:
            sell_watch_active = True
            sell_watch_start = b.date
            log.append(
                f"{b.date} SELL_WATCH armed @ {close:.2f} | breakeven={breakeven:.2f} | reason=weakness-first-signal"
            )

        if sell_watch_active and pos.total_units() > 0:
            confirmed_weakness = i >= 1 and closes[i - 1] < ma5[i - 1] and close < ma5[i]
            watch_invalidated = close > ma5[i] and not overheat
            if watch_invalidated:
                sell_watch_active = False
                log.append(f"{b.date} SELL_WATCH canceled @ {close:.2f} | reason=reclaimed-ma5")
            elif confirmed_weakness and no_loss_ok:
                trim_units = max(0.5, pos.total_units() * 0.35)
                sold = pos.sell(close, units=trim_units, fee_rate=fee_rate, no_loss_only=no_loss_sell)
                if sold > 0:
                    last_confirmed_sell_price = close
                    sell_watch_active = False
                    log.append(
                        f"{b.date} SELL {sold:.2f} @ {close:.2f} | breakeven={breakeven:.2f} | reason=confirmed-breakdown"
                    )

        continuation_breakdown = (
            last_confirmed_sell_price is not None
            and close <= last_confirmed_sell_price * 0.985
            and no_loss_ok
            and pos.total_units() > 0
        )
        if emit_trades and continuation_breakdown:
            trim2_units = max(0.5, pos.total_units() * 0.35)
            sold2 = pos.sell(close, units=trim2_units, fee_rate=fee_rate, no_loss_only=no_loss_sell)
            if sold2 > 0:
                last_confirmed_sell_price = close
                log.append(
                    f"{b.date} SELL {sold2:.2f} @ {close:.2f} | breakeven={breakeven:.2f} | reason=continuation-breakdown"
                )

    log.extend(["", ""] + describe_market_structure(bars, ma5, ma20, upper, lower))

    if chart_output:
        try:
            chart_path = render_signal_chart(
                bars=bars,
                ma5=ma5,
                ma20=ma20,
                upper=upper,
                lower=lower,
                symbol=chart_symbol,
                option_walls=option_walls,
                wall_top_n=option_wall_top_n,
                output_path=chart_output,
            )
            log.append("")
            log.append(f"图表已生成: {chart_path}")
        except Exception as e:  # noqa: BLE001
            log.append("")
            log.append(f"图表生成失败: {e}")

    if emit_snapshot and bars:
        latest = bars[-1]
        log.append("")
        log.append("=== Latest Snapshot ===")
        log.append(f"Date: {latest.date}")
        log.append(f"Close: {latest.close:.2f}")
        log.append(f"Position Units: {pos.total_units():.2f}")
        log.append(f"Avg Cost: {pos.avg_cost():.2f}")
        log.append(f"No-loss Sell Line (fee {fee_rate*100:.2f}%): {pos.breakeven_price(fee_rate):.2f}")
    return log


def run_signal_once(
    symbol: str,
    fills: List[Fill],
    csv_path: Optional[str],
    history_days: int,
    start_date: Optional[str],
    end_date: Optional[str],
    provider: str,
    fee_rate: float,
    min_spacing_pct: float,
    max_total_units: float,
    with_option_wall: bool,
    max_option_exp: int,
    max_option_top: int,
    option_wall_top_n: int,
    option_strike_min_ratio: float,
    option_strike_max_ratio: float,
    emit_trades: bool = True,
    emit_snapshot: bool = True,
    chart_output: Optional[str] = None,
) -> List[str]:
    if not fills:
        raise ValueError("signal模式需要给出仓位明细，请通过 --fills/--fills-csv/--fills-json 提供")

    bars: List[Bar]
    option_walls: Optional[List[PutWall]] = None
    source = "unknown"

    if csv_path:
        bars = parse_csv(csv_path)
        source = f"CSV:{csv_path}"
    else:
        ds = MarketDataClient(provider=provider)
        bars = ds.fetch_ohlcv(
            symbol=symbol,
            days=history_days,
            start_date=start_date,
            end_date=end_date,
        )
        source = f"API:{provider}:{symbol}"

        if with_option_wall:
            try:
                option_walls = ds.fetch_put_walls(
                    symbol=symbol,
                    max_expirations=max_option_exp,
                    top_n=max_option_top,
                    spot_price=bars[-1].close if bars else None,
                    min_strike_ratio=option_strike_min_ratio,
                    max_strike_ratio=option_strike_max_ratio,
                )
            except Exception as e:
                # 期权墙仅用于辅助判断，失败不影响主流程。
                print(f"期权墙抓取失败({provider}): {e}")
                option_walls = []
            if option_walls is None:
                option_walls = []

    if not bars:
        raise RuntimeError(f"未拿到任何K线数据: {source}")

    if csv_path and with_option_wall:
        print("期权墙在 CSV 模式下不可用（仅 API 模式可用）")

    return run_signal_engine(
        bars=bars,
        initial_fills=fills,
        fee_rate=fee_rate,
        min_spacing_pct=min_spacing_pct,
        max_total_units=max_total_units,
        chart_output=chart_output,
        chart_symbol=symbol,
        emit_trades=emit_trades,
        emit_snapshot=emit_snapshot,
        option_wall_top_n=option_wall_top_n,
        option_walls=option_walls,
    )


def run_key_levels_once(
    symbol: str,
    csv_path: Optional[str],
    history_days: int,
    start_date: Optional[str],
    end_date: Optional[str],
    provider: str,
    option_wall_top_n: int,
    with_option_wall: bool,
    max_option_exp: int,
    max_option_top: int,
    option_strike_min_ratio: float,
    option_strike_max_ratio: float,
) -> List[str]:
    if csv_path:
        bars = parse_csv(csv_path)
        source = f"CSV:{csv_path}"
        option_walls: Optional[List[PutWall]] = []
        if with_option_wall:
            print("关键点位仅在期权墙模式支持在线抓数；CSV 模式仅输出前低位。")
    else:
        ds = MarketDataClient(provider=provider)
        bars = ds.fetch_ohlcv(
            symbol=symbol,
            days=history_days,
            start_date=start_date,
            end_date=end_date,
        )
        source = f"API:{provider}:{symbol}"
        option_walls = []
        if with_option_wall:
            option_walls = ds.fetch_put_walls(
                symbol=symbol,
                max_expirations=max_option_exp,
                top_n=max_option_top,
                spot_price=bars[-1].close if bars else None,
                min_strike_ratio=option_strike_min_ratio,
                max_strike_ratio=option_strike_max_ratio,
            )

    if not bars:
        raise RuntimeError(f"未拿到任何K线数据: {source}")

    levels = detect_key_levels(
        bars=bars,
        option_walls=option_walls,
        swing_lookback=60,
        front_low_buf_pct=0.005,
    )

    out: List[str] = []
    out.extend(describe_key_levels(levels, latest_close=bars[-1].close if bars else None))
    if option_walls:
        out.append("")
        out.extend(describe_put_walls(option_walls, max_lines=max(1, option_wall_top_n)))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Technical-level review + signal strategy helper.")
    parser.add_argument("--mode", choices=["review", "signal", "levels"], required=True)
    parser.add_argument("--symbol", default="AAPL", help="Ticker symbol, default AAPL")
    parser.add_argument(
        "--csv",
        help="离线调试用的本地K线文件（默认不启用）。默认仅用于 signal 调试，生产请去掉 --csv",
    )
    parser.add_argument(
        "--history-days",
        type=int,
        default=300,
        help="Auto fetch历史长度（天） when --csv not provided, default 300",
    )
    parser.add_argument(
        "--start-date",
        help="可选：按起始日期过滤（YYYY-MM-DD），用于 signal 模式",
    )
    parser.add_argument(
        "--end-date",
        help="可选：按结束日期过滤（YYYY-MM-DD），用于 signal 模式",
    )
    parser.add_argument("--fills", help='Buy fills, e.g. "530:1,480:1,450:1,364.5:0.5"')
    parser.add_argument("--fills-csv", help='Fill file path, columns include price+units + optional side/date')
    parser.add_argument("--fills-json", help='Fill file path, JSON with [{"price":530,"units":1,"side":"buy","date":"..."}]')
    parser.add_argument(
        "--provider",
        choices=["yfinance", "stooq"],
        default="yfinance",
        help="数据源，可选 yfinance / stooq。stooq 不支持期权墙数据。",
    )
    parser.add_argument(
        "--allow-offline-csv",
        action="store_true",
        help="显式允许 signal 模式使用 --csv（默认不允许）",
    )
    parser.add_argument("--daemon", action="store_true", help="Enable loop mode for scheduled automatic signal runs.")
    parser.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="Loop interval in minutes for daemon mode, minimum 1 minute, default 30",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=0,
        help="Daemon mode max cycles, 0 means keep running",
    )
    parser.add_argument("--fee", type=float, default=0.003, help="One-way fee/slippage estimate, default 0.003")
    parser.add_argument("--min-spacing", type=float, default=0.15, help="Minimum add spacing, default 0.15")
    parser.add_argument("--mark", type=float, help="Current price used in review mode to estimate unrealized PnL")
    parser.add_argument("--with-option-wall", action="store_true", help="Include Put Wall diagnostics (implied from API option chain)")
    parser.add_argument(
        "--analysis-only",
        action="store_true",
        help="只看市场结构/期权分布，不输出加减仓建议",
    )
    parser.add_argument("--max-option-exp", type=int, default=3, help="How many expirations to inspect, default 3")
    parser.add_argument("--max-option-top", type=int, default=1, help="How many put rows per expiration, default 1")
    parser.add_argument(
        "--option-strike-min-ratio",
        type=float,
        default=0.8,
        help="Put strike 下限（按现价比例）; 默认0.8表示最低只取 spot*0.8 及以上",
    )
    parser.add_argument(
        "--option-strike-max-ratio",
        type=float,
        default=1.2,
        help="Put strike 上限（按现价比例）; 默认1.2表示最高只取 spot*1.2 及以下",
    )
    parser.add_argument(
        "--option-wall-top",
        type=int,
        default=8,
        help="期权墙按 OI 聚合后仅展示前 N 档，默认 8",
    )
    parser.add_argument(
        "--chart",
        action="store_true",
        help="生成技术面+期权墙图表并保存为 PNG",
    )
    parser.add_argument("--chart-path", help="指定图表保存路径（默认 auto 生成）")
    parser.add_argument(
        "--signals-only",
        action="store_true",
        help="只输出买卖信号（BUY/SELL），不输出快照和复盘细节",
    )
    args = parser.parse_args()

    try:
        fills = parse_fills(args.fills, csv_path=args.fills_csv, json_path=args.fills_json)
    except Exception as e:
        if args.mode in {"review", "signal"}:
            raise SystemExit(f"交易明细解析失败：{e}")
        fills = []

    if args.mode == "review":
        if not fills:
            raise SystemExit("审视模式需要交易明细，请通过 --fills/--fills-csv/--fills-json 提供")
        print(
            review_report(
                fills,
                min_spacing_pct=args.min_spacing,
                current_price=args.mark,
                fee_rate=args.fee,
            )
        )
        return

    if args.mode == "levels":
        levels = run_key_levels_once(
            symbol=args.symbol,
            csv_path=args.csv,
            history_days=args.history_days,
            start_date=_to_iso_date(args.start_date, "start-date") if args.start_date else None,
            end_date=_to_iso_date(args.end_date, "end-date") if args.end_date else None,
            provider=args.provider,
            option_wall_top_n=args.option_wall_top,
            with_option_wall=args.with_option_wall,
            max_option_exp=args.max_option_exp,
            max_option_top=args.max_option_top,
            option_strike_min_ratio=args.option_strike_min_ratio,
            option_strike_max_ratio=args.option_strike_max_ratio,
        )
        print("\n".join(levels))
        return

    if args.daemon and args.mode != "signal":
        raise SystemExit("daemon 模式仅支持 signal 模式")
    if args.daemon and args.interval <= 0:
        raise SystemExit("--interval 必须为正数")
    if args.daemon and args.interval < 1:
        raise SystemExit("--interval 在daemon模式下最小 1")

    if args.analysis_only and args.signals_only:
        raise SystemExit("--analysis-only 与 --signals-only 不能同时使用")

    if args.mode == "signal" and args.csv and not args.allow_offline_csv:
        raise SystemExit("signal 模式默认不允许 --csv，需去掉 --csv 以在线抓数；如确实要离线调试请加 --allow-offline-csv")

    if not args.csv and not args.symbol:
        raise SystemExit("signal模式必须提供 --csv 或 --symbol 来拉取数据")
    if not fills:
        raise SystemExit("signal模式需要给出仓位明细，请通过 --fills/--fills-csv/--fills-json 提供")

    def _run_once() -> None:
        chart_output = None
        if args.chart:
            if args.chart_path:
                chart_output = args.chart_path
            else:
                chart_output = str(
                    Path.cwd() / f"{args.symbol.lower()}_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                )
        logs = run_signal_once(
            symbol=args.symbol,
            fills=fills,
            csv_path=args.csv,
            history_days=args.history_days,
            start_date=_to_iso_date(args.start_date, "start-date") if args.start_date else None,
            end_date=_to_iso_date(args.end_date, "end-date") if args.end_date else None,
            provider=args.provider,
            fee_rate=args.fee,
            min_spacing_pct=args.min_spacing,
            max_total_units=4.5,
            emit_trades=not args.analysis_only,
            emit_snapshot=not args.analysis_only,
            with_option_wall=args.with_option_wall,
            max_option_exp=args.max_option_exp,
            max_option_top=args.max_option_top,
            option_wall_top_n=args.option_wall_top,
            chart_output=chart_output,
            option_strike_min_ratio=args.option_strike_min_ratio,
            option_strike_max_ratio=args.option_strike_max_ratio,
        )
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{now}] signal run")
        if not args.signals_only:
            print("\n".join(logs))
            return

        signal_lines = [line for line in logs if " BUY " in line or " SELL " in line]
        wall_lines = [line for line in logs if line.startswith("Option wall") or (line.startswith("- ") and "strike" in line)]
        chart_lines = [line for line in logs if line.startswith("图表已生成")]
        if args.with_option_wall and wall_lines:
            print("期权墙参考:")
            print("\n".join(wall_lines[:6]))
            print("")
        if chart_lines:
            print("\n".join(chart_lines))
            print("")
        if signal_lines:
            print("买入/卖出位置:")
            print("\n".join(signal_lines))
        else:
            print("买入/卖出位置: 当前区间未触发")

    if args.daemon:
        count = 0
        while True:
            try:
                _run_once()
            except Exception as e:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{now}] ERROR: {e}")
            count += 1
            if args.max_iterations and count >= args.max_iterations:
                break
            time.sleep(args.interval * 60)
        return

    _run_once()


if __name__ == "__main__":
    main()
