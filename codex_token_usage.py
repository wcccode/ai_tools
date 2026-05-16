#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Codex Token Usage Analyzer 中文版

核心逻辑：
1. 每一条 usage 单独判断长/短上下文
2. 每一条 usage 单独计算费用
3. 再汇总到 session 或 day
4. 避免先汇总 token 后再计费导致长上下文误判

使用：

# 按天统计
python codex_token_usage.py --mode day

# 按会话文件统计
python codex_token_usage.py --mode session

# 指定日志目录
python codex_token_usage.py --root ~/.codex/sessions

# 指定 CSV
python codex_token_usage.py --mode day --csv daily_usage.csv
"""

import json
import csv
import argparse
from pathlib import Path
from collections import defaultdict


# =========================================================
# GPT-5.5 价格配置
# 单位：USD / 1M tokens
# =========================================================

LONG_CONTEXT_THRESHOLD = 272_000

PRICE = {
    "short": {
        "input": 5.0,
        "cached_input": 0.5,
        "output": 30.0,
    },
    "long": {
        "input": 10.0,
        "cached_input": 1.0,
        "output": 45.0,
    },
}


TOKEN_KEYS = [
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "reasoning_output_tokens",
    "total_tokens",
]


def fmt(n):
    return f"{int(n):,}"


def fmt_money(n):
    return f"${n:,.6f}"


def safe_int(v):
    try:
        return int(v)
    except Exception:
        return 0


def empty_usage():
    return defaultdict(int)


def normalize_usage(usage):
    data = {k: safe_int(usage.get(k)) for k in TOKEN_KEYS}

    if data["total_tokens"] == 0:
        data["total_tokens"] = (
            data["input_tokens"]
            + data["output_tokens"]
            + data["reasoning_tokens"]
            + data["reasoning_output_tokens"]
        )

    return data


def find_usage(obj):
    results = []

    if isinstance(obj, dict):
        for key in ["usage", "token_count", "last_token_usage"]:
            value = obj.get(key)

            if isinstance(value, dict):
                if any(k in value for k in TOKEN_KEYS):
                    results.append(normalize_usage(value))

        for value in obj.values():
            results.extend(find_usage(value))

    elif isinstance(obj, list):
        for item in obj:
            results.extend(find_usage(item))

    return results


def get_context_type(usage):
    return "long" if usage["input_tokens"] > LONG_CONTEXT_THRESHOLD else "short"


def get_context_name(context_type):
    return "长上下文" if context_type == "long" else "短上下文"


def estimate_cost_for_usage(usage):
    """
    关键点：
    费用必须按每一次 usage 单独计算。
    不能先汇总到 session/day 后再判断长短上下文。
    """
    context_type = get_context_type(usage)
    price = PRICE[context_type]

    input_tokens = usage["input_tokens"]
    cached_tokens = usage["cached_input_tokens"]

    normal_input_tokens = max(input_tokens - cached_tokens, 0)

    input_cost = normal_input_tokens / 1_000_000 * price["input"]
    cached_cost = cached_tokens / 1_000_000 * price["cached_input"]

    output_like_tokens = (
        usage["output_tokens"]
        + usage["reasoning_tokens"]
        + usage["reasoning_output_tokens"]
    )

    output_cost = output_like_tokens / 1_000_000 * price["output"]

    cost = input_cost + cached_cost + output_cost

    return cost, context_type


def get_date_from_path(file):
    """
    从路径推断日期：
    ~/.codex/sessions/2026/05/16/xxx.jsonl
    """
    parts = file.parts

    try:
        idx = parts.index("sessions")
        year = parts[idx + 1]
        month = parts[idx + 2]
        day = parts[idx + 3]
        return f"{year}-{month}-{day}"
    except Exception:
        return "unknown"


def add_usage(target, usage):
    for key in TOKEN_KEYS:
        target[key] += usage.get(key, 0)


def make_empty_group(key, date):
    return {
        "key": key,
        "date": date,
        "usage": empty_usage(),
        "cost": 0.0,
        "request_count": 0,
        "short_count": 0,
        "long_count": 0,
    }


def add_request_to_group(group, usage, cost, context_type):
    add_usage(group["usage"], usage)

    group["cost"] += cost
    group["request_count"] += 1

    if context_type == "long":
        group["long_count"] += 1
    else:
        group["short_count"] += 1


def get_context_summary(group):
    short_count = group["short_count"]
    long_count = group["long_count"]

    if short_count > 0 and long_count > 0:
        return f"混合：短{short_count} / 长{long_count}"

    if long_count > 0:
        return f"长上下文：{long_count}"

    return f"短上下文：{short_count}"


def scan_logs(root):
    """
    扫描所有 jsonl。
    返回：
    1. session 维度统计
    2. day 维度统计

    注意：
    每条 usage 出现时立即计算费用，再分别累计到 session/day。
    """
    root = Path(root).expanduser()

    if not root.exists():
        raise SystemExit(f"未找到日志目录：{root}")

    session_stats = {}
    day_stats = {}

    seen = set()

    for file in root.glob("**/*.jsonl"):
        session_key = str(file.relative_to(root))
        day_key = get_date_from_path(file)

        if session_key not in session_stats:
            session_stats[session_key] = make_empty_group(session_key, day_key)

        if day_key not in day_stats:
            day_stats[day_key] = make_empty_group(day_key, day_key)

        try:
            with open(file, "r", encoding="utf-8", errors="ignore") as f:
                for line_no, line in enumerate(f, 1):
                    line = line.strip()

                    if not line:
                        continue

                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue

                    usages = find_usage(obj)

                    for usage in usages:
                        dedupe_key = (
                            str(file),
                            line_no,
                            json.dumps(usage, sort_keys=True),
                        )

                        if dedupe_key in seen:
                            continue

                        seen.add(dedupe_key)

                        cost, context_type = estimate_cost_for_usage(usage)

                        add_request_to_group(
                            session_stats[session_key],
                            usage,
                            cost,
                            context_type,
                        )

                        add_request_to_group(
                            day_stats[day_key],
                            usage,
                            cost,
                            context_type,
                        )

        except Exception as e:
            print(f"读取失败：{file}，原因：{e}")

    session_stats = {
        k: v for k, v in session_stats.items()
        if v["request_count"] > 0
    }

    day_stats = {
        k: v for k, v in day_stats.items()
        if v["request_count"] > 0
    }

    return session_stats, day_stats


def build_rows(data):
    rows = []
    grand_total = empty_usage()
    grand_cost = 0.0
    grand_request_count = 0
    grand_short_count = 0
    grand_long_count = 0

    sorted_items = sorted(
        data.items(),
        key=lambda x: x[0],
        reverse=True,
    )

    for key, item in sorted_items:
        usage = item["usage"]

        reasoning = (
            usage["reasoning_tokens"]
            + usage["reasoning_output_tokens"]
        )

        row = {
            "维度": key,
            "请求数": item["request_count"],
            "上下文类型": get_context_summary(item),
            "输入Token": usage["input_tokens"],
            "缓存输入Token": usage["cached_input_tokens"],
            "输出Token": usage["output_tokens"],
            "推理Token": reasoning,
            "总Token": usage["total_tokens"],
            "预估费用USD": item["cost"],
        }

        rows.append(row)

        add_usage(grand_total, usage)
        grand_cost += item["cost"]
        grand_request_count += item["request_count"]
        grand_short_count += item["short_count"]
        grand_long_count += item["long_count"]

    grand = {
        "usage": grand_total,
        "cost": grand_cost,
        "request_count": grand_request_count,
        "short_count": grand_short_count,
        "long_count": grand_long_count,
    }

    return rows, grand


def print_report(rows, grand, mode):
    title = "CODEX 每日 Token 消耗统计" if mode == "day" else "CODEX 会话 Token 消耗统计"

    print(f"\n========== {title} ==========\n")

    for row in rows:
        print(
            f"{row['维度']} | "
            f"请求数={fmt(row['请求数'])} | "
            f"上下文={row['上下文类型']} | "
            f"输入={fmt(row['输入Token'])} | "
            f"缓存输入={fmt(row['缓存输入Token'])} | "
            f"输出={fmt(row['输出Token'])} | "
            f"推理={fmt(row['推理Token'])} | "
            f"总计={fmt(row['总Token'])} | "
            f"费用={fmt_money(row['预估费用USD'])}"
        )

    print("\n============== 总计 ==============\n")

    usage = grand["usage"]

    total_reasoning = (
        usage["reasoning_tokens"]
        + usage["reasoning_output_tokens"]
    )

    print(f"请求数: {fmt(grand['request_count'])}")
    print(f"短上下文请求数: {fmt(grand['short_count'])}")
    print(f"长上下文请求数: {fmt(grand['long_count'])}")
    print(f"输入 Token: {fmt(usage['input_tokens'])}")
    print(f"缓存输入 Token: {fmt(usage['cached_input_tokens'])}")
    print(f"输出 Token: {fmt(usage['output_tokens'])}")
    print(f"推理 Token: {fmt(total_reasoning)}")
    print(f"总 Token: {fmt(usage['total_tokens'])}")
    print(f"预估费用 USD: {fmt_money(grand['cost'])}")


def write_csv(rows, csv_file):
    fieldnames = [
        "维度",
        "请求数",
        "上下文类型",
        "输入Token",
        "缓存输入Token",
        "输出Token",
        "推理Token",
        "总Token",
        "预估费用USD",
    ]

    with open(csv_file, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for row in rows:
            writer.writerow({
                "维度": row["维度"],
                "请求数": fmt(row["请求数"]),
                "上下文类型": row["上下文类型"],
                "输入Token": fmt(row["输入Token"]),
                "缓存输入Token": fmt(row["缓存输入Token"]),
                "输出Token": fmt(row["输出Token"]),
                "推理Token": fmt(row["推理Token"]),
                "总Token": fmt(row["总Token"]),
                "预估费用USD": fmt_money(row["预估费用USD"]),
            })

    print(f"\nCSV 文件已生成：{csv_file}")


def main():
    parser = argparse.ArgumentParser(
        description="Codex Token Usage Analyzer 中文版"
    )

    parser.add_argument(
        "--mode",
        choices=["day", "session"],
        default="day",
        help="统计维度：day=按天，session=按会话文件",
    )

    parser.add_argument(
        "--root",
        default=str(Path.home() / ".codex" / "sessions"),
        help="Codex sessions 日志目录",
    )

    parser.add_argument(
        "--csv",
        default="usage_report.csv",
        help="导出 CSV 文件名",
    )

    args = parser.parse_args()

    session_stats, day_stats = scan_logs(args.root)

    data = day_stats if args.mode == "day" else session_stats

    rows, grand = build_rows(data)

    print_report(rows, grand, args.mode)

    write_csv(rows, args.csv)


if __name__ == "__main__":
    main()
