"""检索引擎适配器（spec-2026-09-25-retrieval §4 / §7）。

全部走标准库（urllib + html.parser + xml.etree），零新增依赖；HTTP 一律经
`core.httputil.open_bounded`（禁重定向 + 限量读取）。引擎端点是**宿主常量**，
是白名单自动添加（用户裁决 W1）的唯一来源。

baidu / bing 为无 key 解析方案（best-effort）：官方改版或风控即失效——
报错不崩、不重试对抗（spec §4 / §15）。
"""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from xml.etree import ElementTree

from core.httputil import open_bounded
from shared.net import is_secure_transport

#: 引擎注册序（default_engines 为空时的默认序）。
ENGINE_ORDER = ("webfetch", "duckduckgo", "baidu", "bing", "arxiv", "exa", "tavily")

#: 每个引擎的官方端点域名（白名单自动添加的唯一来源；webfetch 无固定端点）。
ENGINE_ENDPOINTS: dict[str, tuple[str, ...]] = {
    "webfetch": (),
    "duckduckgo": ("html.duckduckgo.com",),
    "baidu": ("www.baidu.com",),
    "bing": ("www.bing.com",),
    "arxiv": ("export.arxiv.org",),
    "exa": ("api.exa.ai",),
    "tavily": ("api.tavily.com",),
}

#: 需要 API key 的引擎（key = vault 确定性命名 api_key/retrieval.<engine>）。
NEEDS_KEY = frozenset({"exa", "tavily"})

#: 每引擎最小调用间隔（秒；arxiv 3s 为官方硬性礼貌速率）。
MIN_INTERVAL_S: dict[str, float] = {
    "webfetch": 0.0,
    "duckduckgo": 2.0,
    "baidu": 2.0,
    "bing": 2.0,
    "arxiv": 3.0,
    "exa": 1.0,
    "tavily": 1.0,
}

#: 诚实标识 UA（不伪装浏览器；spec §9）。
USER_AGENT = "YMTAgents/0.0 (retrieval; personal agent workbench)"

SNIPPET_CHARS = 300
DEFAULT_TIMEOUT_S = 15.0


class RetrievalError(RuntimeError):
    """检索层异常，携带错误码（复用既有码，一码一义）。"""

    def __init__(self, message: str, code: str = "tool_backend_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Hit:
    """单条搜索结果（进提示词前经截断与 redact）。"""

    title: str
    url: str
    snippet: str = ""


# ---------------------------------------------------------------------------
# HTTP 基元（统一错误归码）
# ---------------------------------------------------------------------------
def _request(
    url: str,
    *,
    data: dict | None = None,
    json_body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> urllib.request.Request:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if json_body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(json_body).encode("utf-8")
    elif data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.data = urllib.parse.urlencode(data).encode("utf-8")
    return req


def _open(req: urllib.request.Request, timeout: float) -> tuple[str, object]:
    """open_bounded 的统一错误归码包装（HTTPError/URLError/超时）。"""
    try:
        return open_bounded(req, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RetrievalError(f"引擎拒绝了请求（HTTP {exc.code}），请检查 API key", "auth_error") from None
        if exc.code == 429:
            raise RetrievalError("引擎限流（HTTP 429），请稍后再试", "tool_backend_error") from None
        raise RetrievalError(f"引擎返回 HTTP {exc.code}", "tool_backend_error") from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise RetrievalError("引擎请求超时", "tool_timeout") from None
        raise RetrievalError(f"网络错误：{reason}", "network_error") from None
    except (TimeoutError, socket.timeout):
        raise RetrievalError("引擎请求超时", "tool_timeout") from None


def _trim(text: str, limit: int = SNIPPET_CHARS) -> str:
    text = " ".join(text.split())
    return text[:limit]


# ---------------------------------------------------------------------------
# HTML 解析（html.parser 子类；禁止对 HTML 结构用正则，spec §4）
# ---------------------------------------------------------------------------
class _Anchors(HTMLParser):
    """收集全部 <a>（href/class/文本），供各引擎按类筛选。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self._href: str | None = None
        self._classes: list[str] = []
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            d = dict(attrs)
            self._href = d.get("href")
            self._classes = (d.get("class") or "").split()
            self._buf = []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._buf).split())
            if text or self._href:
                self.items.append({"href": self._href, "classes": self._classes, "text": text})
            self._href = None
            self._classes = []
            self._buf = []


def _unwrap_ddg(href: str | None) -> str:
    """还原 DuckDuckGo 的 /l/?uddg=<encoded> 跳转链接。"""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    if "/l/?" in href:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        uddg = (qs.get("uddg") or [""])[0]
        if uddg:
            href = uddg
    return href


def _absolute(href: str, base: str) -> str:
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return base + href
    return href


class _TextBlocks(HTMLParser):
    """网页正文抽取（外部数据，有界文本化；同时取 <title>）。"""

    SKIP = {"script", "style", "noscript", "svg", "template"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "section", "article", "table", "ul", "ol"}

    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._chunks: list[str] = []
        self._len = 0
        self.truncated = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title = " ".join((self.title + data).split())[:200]
            return
        if self._len >= self.limit:
            self.truncated = True
            return
        take = data[: self.limit - self._len]
        self._chunks.append(take)
        self._len += len(take)
        if len(data) > len(take):
            self.truncated = True


def _extract_text(html: str, limit: int) -> tuple[str, str, bool]:
    parser = _TextBlocks(limit)
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - 外部 HTML 解析失败不外溢
        return "", parser.title, False
    return "".join(parser._chunks).strip(), parser.title, parser.truncated  # noqa: SLF001

class _Baidu(HTMLParser):
    """best-effort 解析百度结果页：标题在 <h3><a>，摘要在 class 含 c-abstract 的容器。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self.abstracts: list[str] = []
        self._h3 = 0
        self._url: str | None = None
        self._title_buf: list[str] = []
        self._abs_depth = 0
        self._abs_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        if tag == "h3":
            self._h3 += 1
        elif tag == "a" and self._h3 and self._url is None:
            self._url = dict(attrs).get("href")
        elif tag == "div" and any("c-abstract" in c for c in classes):
            self._abs_depth += 1

    def handle_data(self, data):
        if self._url is not None:
            self._title_buf.append(data)
        elif self._abs_depth:
            self._abs_buf.append(data)

    def handle_endtag(self, tag):
        if tag == "h3" and self._h3:
            self._h3 -= 1
            title = " ".join("".join(self._title_buf).split())
            if self._url and title:
                self.items.append({"url": self._url, "title": title})
            self._url = None
            self._title_buf = []
        elif tag == "div" and self._abs_depth:
            self._abs_depth -= 1
            text = " ".join("".join(self._abs_buf).split())
            if text:
                self.abstracts.append(text)
            self._abs_buf = []


def _search_baidu(query: str, count: int, key: str | None, timeout: float) -> list[Hit]:
    req = _request("https://www.baidu.com/s", data={"wd": query, "rn": str(count)})
    body, _h = _open(req, timeout)
    parser = _Baidu()
    try:
        parser.feed(body)
    except Exception:  # noqa: BLE001 - 外部 HTML 解析失败不外溢
        pass
    if not parser.items:
        raise RetrievalError("搜索引擎未返回可解析的结果（可能被风控或改版）", "tool_backend_error")
    return [
        Hit(item["title"], item["url"], _trim(parser.abstracts[i] if i < len(parser.abstracts) else ""))
        for i, item in enumerate(parser.items[:count])
    ]


class _Bing(HTMLParser):
    """best-effort 解析 Bing 结果页：li.b_algo 内 h2>a 标题 + p 摘要。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self.snippets: list[str] = []
        self._algo = 0
        self._h2 = 0
        self._url: str | None = None
        self._title_buf: list[str] = []
        self._p_depth = 0
        self._p_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        if tag == "li" and "b_algo" in classes:
            self._algo += 1
        elif tag == "h2" and self._algo:
            self._h2 += 1
        elif tag == "a" and self._algo and self._h2 and self._url is None:
            self._url = dict(attrs).get("href")
        elif tag == "p" and self._algo:
            self._p_depth += 1

    def handle_data(self, data):
        if self._url is not None:
            self._title_buf.append(data)
        elif self._p_depth:
            self._p_buf.append(data)

    def handle_endtag(self, tag):
        if tag == "h2" and self._h2:
            self._h2 -= 1
            title = " ".join("".join(self._title_buf).split())
            if self._url and title:
                self.items.append({"url": self._url, "title": title})
            self._url = None
            self._title_buf = []
        elif tag == "p" and self._p_depth:
            self._p_depth -= 1
            text = " ".join("".join(self._p_buf).split())
            if text:
                self.snippets.append(text)
        elif tag == "li" and self._algo:
            self._algo -= 1


def _search_bing(query: str, count: int, key: str | None, timeout: float) -> list[Hit]:
    req = _request("https://www.bing.com/search", data={"q": query, "count": str(count)})
    body, _h = _open(req, timeout)
    parser = _Bing()
    try:
        parser.feed(body)
    except Exception:  # noqa: BLE001
        pass
    if not parser.items:
        raise RetrievalError("搜索引擎未返回可解析的结果（可能被风控或改版）", "tool_backend_error")
    return [
        Hit(item["title"], item["url"], _trim(parser.snippets[i] if i < len(parser.snippets) else ""))
        for i, item in enumerate(parser.items[:count])
    ]


def _search_arxiv(query: str, count: int, key: str | None, timeout: float) -> list[Hit]:
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {"search_query": f"all:{query}", "start": "0", "max_results": str(count), "sortBy": "relevance"}
    )
    body, _h = _open(_request(url), timeout)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        raise RetrievalError("arXiv 返回无法解析（API 变更或限流）", "tool_backend_error") from None
    hits: list[Hit] = []
    for entry in root.findall("a:entry", ns):
        title = _trim(entry.findtext("a:title", default="", namespaces=ns))
        link = (entry.findtext("a:id", default="", namespaces=ns) or "").strip()
        summary = _trim(entry.findtext("a:summary", default="", namespaces=ns) or "")
        if title and link:
            hits.append(Hit(title, link.replace("http://", "https://", 1), summary))
    if not hits:
        raise RetrievalError("arXiv 未返回结果", "tool_backend_error")
    return hits


def _parse_json_results(body: str, count: int) -> list[Hit]:
    try:
        doc = json.loads(body)
    except (ValueError, TypeError):
        raise RetrievalError("引擎返回无法解析", "tool_backend_error") from None
    rows = doc.get("results") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        raise RetrievalError("引擎响应缺少 results", "tool_backend_error")
    hits: list[Hit] = []
    for row in rows[:count]:
        if not isinstance(row, dict):
            continue
        title = _trim(str(row.get("title") or ""))
        url = str(row.get("url") or "")
        snippet = str(row.get("content") or row.get("text") or row.get("snippet") or "")
        if url:
            hits.append(Hit(title, url, _trim(snippet)))
    return hits


def _search_exa(query: str, count: int, key: str | None, timeout: float) -> list[Hit]:
    req = _request(
        "https://api.exa.ai/search",
        json_body={"query": query, "numResults": count},
        headers={"x-api-key": key or "", "Accept": "application/json"},
    )
    body, _h = _open(req, timeout)
    hits = _parse_json_results(body, count)
    if not hits:
        raise RetrievalError("引擎未返回结果", "tool_backend_error")
    return hits


def _search_tavily(query: str, count: int, key: str | None, timeout: float) -> list[Hit]:
    req = _request(
        "https://api.tavily.com/search",
        json_body={"query": query, "max_results": count},
        headers={"Authorization": f"Bearer {key or ''}", "Accept": "application/json"},
    )
    body, _h = _open(req, timeout)
    hits = _parse_json_results(body, count)
    if not hits:
        raise RetrievalError("引擎未返回结果", "tool_backend_error")
    return hits


def _search_duckduckgo(query: str, count: int, key: str | None, timeout: float) -> list[Hit]:
    req = _request("https://html.duckduckgo.com/html/", data={"q": query})
    body, _h = _open(req, timeout)
    anchors = _Anchors()
    try:
        anchors.feed(body)
    except Exception:  # noqa: BLE001 - 外部 HTML 解析失败不外溢
        pass
    titles = [i for i in anchors.items if "result__a" in i["classes"]]
    snippets = [i["text"] for i in anchors.items if "result__snippet" in i["classes"]]
    hits: list[Hit] = []
    for i, item in enumerate(titles):
        url = _unwrap_ddg(item["href"])
        if not url.startswith("https://"):
            continue
        hits.append(Hit(item["text"], url, _trim(snippets[i] if i < len(snippets) else "")))
        if len(hits) >= count:
            break
    if not hits:
        raise RetrievalError("搜索引擎未返回可解析的结果（可能被风控）", "tool_backend_error")
    return hits


#: 引擎名 → 搜索实现（webfetch 不是搜索引擎，仅用于 fetch）。
_SEARCHERS = {
    "duckduckgo": _search_duckduckgo,
    "baidu": _search_baidu,
    "bing": _search_bing,
    "arxiv": _search_arxiv,
    "exa": _search_exa,
    "tavily": _search_tavily,
}


def search(engine: str, query: str, count: int, key: str | None = None, timeout: float = DEFAULT_TIMEOUT_S) -> list[Hit]:
    """统一入口；key 由 manager 经注入的密钥解析器取得，绝不入日志/事件。"""
    fn = _SEARCHERS.get(engine)
    if fn is None:
        raise RetrievalError(f"未知引擎：{engine}", "tool_invalid_args")
    if engine in NEEDS_KEY and not key:
        raise RetrievalError("该引擎需要 API key，请在设置页填入", "key_missing")
    return fn(query, count, key, timeout)


# ---------------------------------------------------------------------------
# 网页抓取（webfetch；spec §7.3 / §8）
# ---------------------------------------------------------------------------
def classify_host(host: str) -> str:
    """'loopback' | 'private' | 'public'（best-effort；解析失败按 public，白名单仍兜底）。"""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return "public"
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            addrs.append(ipaddress.ip_address(info[4][0]))  # type: ignore[arg-type]
        except ValueError:
            continue
    if not addrs:
        return "public"
    if any(a.is_loopback for a in addrs):
        return "loopback"
    if any(a.is_private or a.is_link_local for a in addrs):
        return "private"
    return "public"


def fetch(
    url: str,
    max_chars: int,
    whitelist: object,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """抓取网页正文（真·任意出口：必须 https + 白名单；回环一律拒绝，spec §7.3）。"""
    if not url.lower().startswith("https://") or not is_secure_transport(url):
        raise RetrievalError("网页抓取仅允许 https 地址", "insecure_transport")
    host = urllib.parse.urlparse(url).hostname or ""
    if not host:
        raise RetrievalError("URL 缺少主机名", "tool_invalid_args")
    if classify_host(host) == "loopback":
        raise RetrievalError("本机回环地址不允许抓取", "whitelist_blocked")
    if not whitelist.is_allowed(url):
        raise RetrievalError(f"域名不在出口白名单：{host}", "whitelist_blocked")
    req = _request(url, headers={"Accept": "text/html,application/xhtml+xml;q=0.8,*/*;q=0.5"})
    body, _h = _open(req, timeout)
    text, title, truncated = _extract_text(body, max_chars)
    if not text:
        raise RetrievalError("网页未解析出正文（可能是脚本渲染或非 HTML 页面）", "tool_backend_error")
    return {"url": url, "host": host, "title": title, "text": text, "truncated": truncated}
