"""受限的联网解决方案搜索。"""

import os
import re
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


class WebSearchError(RuntimeError):
    """搜索配置无效或搜索服务不可用。"""


def _safe_query(service_id: str, query: str) -> str:
    if not isinstance(query, str) or not query.strip():
        raise WebSearchError("搜索内容不能为空")
    if len(query) > 300:
        raise WebSearchError("搜索内容不能超过 300 个字符")

    # 发送前去掉常见密钥和密码。
    redacted = re.sub(r"(?i)bearer\s+\S+", "Bearer [REDACTED]", query)
    redacted = re.sub(r"(?i)(api[_-]?key|token|password)\s*[:=]\s*\S+", r"\1=[REDACTED]", redacted)
    redacted = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", redacted)
    return f"{service_id} {redacted.strip()} 解决方法"


async def search_web_solutions(service_id: str, query: str) -> dict:
    """搜索公开解决方案，结果只作参考。"""
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise WebSearchError("未配置 TAVILY_API_KEY")

    from langchain_tavily import TavilySearch

    tool = TavilySearch(api_key=api_key, max_results=4)
    raw = await tool.ainvoke({"query": _safe_query(service_id, query)})
    rows = raw.get("results", []) if isinstance(raw, dict) else raw
    rows = rows if isinstance(rows, list) else []
    results = []
    for item in rows[:4]:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        url = str(item["url"])[:2000]
        if urlparse(url).scheme not in {"http", "https"}:
            continue
        results.append({
            "title": str(item.get("title") or "未命名结果")[:300],
            "url": url,
            "summary": str(item.get("content") or item.get("snippet") or "")[:1000],
        })

    return {
        "service_id": service_id,
        "source": "tavily/web",
        "status": "returned" if results else "no_data",
        "query": query,
        "results": results,
        "searched_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "limitations": [
            "联网搜索结果仅作排障参考，未验证是否适用于当前版本和环境。",
            "搜索结果不能单独支持重启、扩容或其他修改操作。",
        ],
    }
