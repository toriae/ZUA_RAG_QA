"""反馈统计模块 — 基于 JSON 文件的轻量级反馈记录与聚合。"""

import json
import os
import time
from pathlib import Path

FEEDBACK_FILE = os.environ.get(
    "ZUA_FEEDBACK_FILE",
    str(Path(__file__).resolve().parent.parent / "data" / "feedback.json"),
)

MAX_RECORDS = 5000


def _load() -> list:
    if not os.path.exists(FEEDBACK_FILE):
        return []
    try:
        with open(FEEDBACK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def _save(records: list) -> None:
    os.makedirs(os.path.dirname(FEEDBACK_FILE), exist_ok=True)
    with open(FEEDBACK_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def record_feedback(
    session_id: str,
    intent: str,
    query: str,
    query_length: int,
    retrieved_chars: int,
    missed: bool = False,
) -> None:
    """追加一条反馈记录，超出 MAX_RECORDS 时淘汰最早的一半。"""
    records = _load()
    records.append({
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "session_id": session_id,
        "intent": intent,
        "query": query[:200],
        "query_length": query_length,
        "retrieved_chars": retrieved_chars,
        "missed": missed,
    })
    if len(records) > MAX_RECORDS:
        records = records[-(MAX_RECORDS // 2):]
    _save(records)


def get_stats() -> dict:
    """返回统计摘要 JSON。"""
    records = _load()
    total = len(records)
    missed = sum(1 for r in records if r.get("missed"))

    intent_counts: dict = {}
    for r in records:
        intent = r.get("intent", "UNKNOWN")
        intent_counts[intent] = intent_counts.get(intent, 0) + 1
    top_intents = sorted(intent_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    missed_queries = [
        {"query": r["query"], "timestamp": r["timestamp"], "intent": r.get("intent")}
        for r in records
        if r.get("missed")
    ][-50:]

    return {
        "total": total,
        "missed": missed,
        "hit_rate": round((total - missed) / total * 100, 1) if total else 0.0,
        "top_intents": [{"intent": k, "count": v} for k, v in top_intents],
        "missed_queries": missed_queries,
    }


def clear_feedback() -> None:
    """清空所有反馈记录。"""
    _save([])
