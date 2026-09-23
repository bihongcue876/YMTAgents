"""MCP 安全检测项（A1–A4 / B1–B4），提取自原型 `detection/` 并去 GUI 化。

每个检查函数签名统一为 ``(config, client=None) -> ItemResult``：

- 静态项不触碰 ``client``（传 None 亦可）；
- 主动探测项在无活连接时返回 ``status="skip"``（不报错）；
- 检查函数纯同步、无副作用；网络探测仅针对**用户已配置的 URL**，
  且受 ``shared.net.is_secure_transport`` 传输约束，不新开任意出口。

状态四值：``pass | warn | fail | skip``。
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from shared.net import is_secure_transport

from core.mcp.security import primitives as P

#: B2 一次最多实际调用的只读工具数量，避免慢工具拖垮整次检测。
MAX_RESULT_CALLS = 10

NAME = {
    "A1": "TLS/明文传输",
    "A2": "匿名访问",
    "A3": "硬编码凭证",
    "A4": "协议握手与错误处理",
    "B1": "工具元数据提示注入",
    "B2": "工具结果响应注入",
    "B3": "参数 Schema 约束不足",
    "B4": "危险能力声明不一致",
}

#: 危险能力关键词库。
DANGEROUS_WORDS = [
    "delete", "remove", "drop", "destroy", "kill", "shutdown",
    "exec", "execute", "run", "shell", "command", "eval", "system",
    "write", "put", "post", "upload", "create", "update", "insert",
]
#: 只读特征关键词。
READONLY_HINTS = ["get", "list", "read", "query", "fetch", "search", "view", "show", "find"]
#: 删除类关键词。
DELETE_WORDS = ["delete", "remove", "drop", "destroy", "clear", "purge"]


@dataclass(frozen=True)
class ItemResult:
    """单项检测结果（内部载体；事件载荷由 scanner 组装）。"""

    item_id: str
    item_name: str
    status: str
    evidence: str = ""
    suggestion: str = ""


def _r(item_id: str, status: str, evidence: str = "", suggestion: str = "") -> ItemResult:
    return ItemResult(item_id, NAME[item_id], status, evidence, suggestion)


def _timeout(config) -> int:
    return max(1, int((getattr(config, "timeout_ms", 0) or 10000) / 1000))


# ---------------------------------------------------------------------------
# A 组：传输与鉴权
# ---------------------------------------------------------------------------
def check_tls(config, client=None) -> ItemResult:
    """A1：传输是否加密、证书是否有效（静态 + 真实 TLS 握手）。"""
    if config.transport == "stdio":
        return _r("A1", "skip", "本地进程，不涉及网络传输", "stdio 模式不涉及 TLS")
    url = (config.url or "").strip()
    if not url:
        return _r("A1", "warn", "配置缺少 URL", "请补充服务器地址")
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        scheme = (parsed.scheme or "").lower()
        port = parsed.port or 443
    except ValueError:
        return _r("A1", "fail", "URL 端口格式错误", "请修正 URL 格式")
    if scheme == "http":
        return _r("A1", "fail", f"明文使用 HTTP（主机 {host}）", "建议改用 HTTPS 并配置有效证书")
    if scheme != "https":
        return _r("A1", "warn", f"未知协议：{scheme or '（无）'}", "建议使用 HTTPS")
    if not host:
        return _r("A1", "fail", "HTTPS 地址缺少主机名", "请检查 URL 有效性")
    status, evidence, suggestion = _tls_probe(host, port, _timeout(config))
    return _r("A1", status, evidence, suggestion)


def _tls_probe(host: str, port: int, timeout: int) -> tuple[str, str, str]:
    """执行一次真实 TLS 握手，验证证书情况。"""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert() or {}
                subject = _cert_name(cert.get("subject", []))
                not_after = cert.get("notAfter", "")
                return (
                    "pass",
                    f"证书有效；主机={host}；颁发给={subject}；到期={not_after}",
                    "证书有效，请定时关注证书有效性",
                )
    except ssl.SSLCertVerificationError as exc:
        msg = getattr(exc, "verify_message", "") or str(exc)
        low = msg.lower()
        if "expired" in low:
            return ("fail", f"证书已过期：{msg}", "建议更换有效证书")
        if "not yet valid" in low:
            return ("fail", f"证书尚未生效：{msg}", "建议检查证书生效时间")
        if "hostname" in low or "doesn't match" in low:
            return ("fail", f"证书主机名不匹配：{msg}", "建议更换匹配主机名的证书")
        if "self-signed" in low or "self signed" in low:
            return ("fail", f"自签名或不受信任证书：{msg}", "建议使用可信 CA 签发的证书")
        return ("fail", f"证书验证失败：{msg}", "建议检查证书链")
    except ssl.SSLError as exc:
        return ("fail", f"TLS 握手失败：{exc}", "建议检查服务器 TLS 配置")
    except socket.timeout:
        return ("warn", f"连接超时（{timeout}s），无法完成 TLS 校验", "建议检查网络连通性与地址可达性")
    except socket.gaierror as exc:
        return ("warn", f"DNS 解析失败：{exc}", "建议检查域名是否可解析")
    except OSError as exc:
        return ("warn", f"网络错误：{exc}", "建议检查目标可达性")


def _cert_name(pairs) -> str:
    for item in pairs or []:
        for key, value in item:
            if key == "commonName":
                return value
    return "unknown"


def check_anonymous(config, client=None) -> ItemResult:
    """A2：剥离凭证后能否访问（主动探测 initialize）。"""
    if config.transport == "stdio":
        return _r("A2", "skip", "本地进程不涉及远程鉴权", "stdio 模式无需远程认证")
    url = (config.url or "").strip()
    if not url:
        return _r("A2", "warn", "配置缺少 URL，无法验证", "请补充服务器地址后重试")
    if not is_secure_transport(url):
        return _r("A2", "skip", "非 https 传输，按传输保密约束跳过匿名探测", "请改用 HTTPS")
    code, body = _anonymous_probe(url, _timeout(config))
    if code == -1:
        return _r("A2", "warn", "连接超时，无法验证", "建议检查网络与地址")
    if code == -2:
        return _r("A2", "warn", f"连接失败：{P.truncate(body, 120)}", "建议检查服务可达性")
    if code == 401:
        return _r("A2", "pass", "返回状态码 401，要求鉴权", "服务器具有鉴权机制")
    if code == 403:
        return _r("A2", "pass", "返回状态码 403，拒绝匿名访问", "服务器具有匿名拒绝策略")
    if 200 <= code < 300:
        return _r(
            "A2", "warn",
            f"无凭证即可访问（HTTP {code}），响应片段：{P.truncate(body, 120)}",
            "建议确认是否需要添加鉴权",
        )
    if 500 <= code < 600:
        return _r("A2", "warn", f"服务器错误 HTTP {code}，无法判断鉴权", "建议先修复服务端错误")
    return _r("A2", "warn", f"返回 HTTP {code}，无法明确判断", "建议人工检查")


def _anonymous_probe(url: str, timeout: int) -> tuple[int, str]:
    """发送一条匿名 initialize 请求，返回 (状态码, 响应片段)。"""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2026-07-28",
            "capabilities": {},
            "clientInfo": {"name": "ymt-security-scan", "version": "0.0.1"},
        },
    }).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return int(getattr(resp, "status", 0) or 0), resp.read(300).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(300).decode("utf-8", "replace")
        except Exception:
            body = ""
        return int(exc.code), body
    except socket.timeout:
        return -1, "timeout"
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        if "timed out" in reason.lower():
            return -1, "timeout"
        return -2, reason
    except OSError as exc:
        return -2, str(exc)


def check_credentials(config, client=None) -> ItemResult:
    """A3：env / URL / headers_ref 是否明文存放凭证（静态，值掩码）。"""
    hits: list[tuple[str, str, str]] = []
    hits += _scan_dict(config.env or {}, "env")
    hits += _scan_url(config.url or "")
    hits += _scan_header_refs(config.headers_ref or {})
    if not hits:
        return _r("A3", "pass", "未在 env / URL / 请求头引用中发现明文凭证", "凭证应仅以 vault:// 引用存放")
    lines = [f"{where}.{key}={P.mask(value)}" for where, key, value in hits]
    return _r("A3", "fail", "发现明文凭证：" + "；".join(lines), "建议改用本地加密库（vault://）引用")


def _scan_dict(mapping: dict, label: str) -> list[tuple[str, str, str]]:
    hits = []
    for key, value in mapping.items():
        if not isinstance(value, str):
            continue
        if P.has_hardcoded_secret(f"{key}: {value}"):
            hits.append((label, key, value))
    return hits


def _scan_url(url: str) -> list[tuple[str, str, str]]:
    hits = []
    parsed = urlparse(url)
    if not parsed.query:
        return hits
    sensitive = ("token", "api_key", "apikey", "api-key", "password", "passwd", "secret", "key")
    for pair in parsed.query.split("&"):
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        if key.lower() in sensitive:
            hits.append(("url", key, value))
    return hits


def _scan_header_refs(refs: dict) -> list[tuple[str, str, str]]:
    """请求头引用**只允许 vault:// 形态**；内联值一律视为明文凭证。"""
    hits = []
    for key, value in refs.items():
        if not isinstance(value, str) or not value.strip():
            continue
        if not value.strip().startswith("vault://"):
            hits.append(("headers_ref", key, value))
    return hits


# ---------------------------------------------------------------------------
# A4：协议握手 + 错误响应栈泄露
# ---------------------------------------------------------------------------
_STACK_PATTERNS = [
    r"Traceback \(most recent call last\)",
    r"File\s+\"[^\"]+\.py\"",
    r"File\s+'[^']+\.py'",
    r"[A-Za-z]:\\[^\s\"']+\.py",
    r"/(?:usr|home|var|opt|root)/[^\s\"']+\.py",
    r"site-packages",
    r"line\s+\d+,\s+in\s+\w+",
    r"\bfastmcp\b[\s\S]{0,40}\d+\.\d+",
    r"\buvicorn\b",
    r"\bstarlette\b",
    r"\bpydantic\b[\s\S]{0,40}\d+\.\d+",
]
_STACK_REGEX = [re.compile(p) for p in _STACK_PATTERNS]


def check_handshake(config, client=None) -> ItemResult:
    """A4：协议握手 + 错误响应是否泄露内部信息（主动探测，需活连接）。"""
    if client is None or not getattr(client, "connected", False):
        return _r("A4", "skip", "无已连接客户端，主动探测不适用", "连接服务器后就地复检")
    version = str(getattr(client, "protocol_version", "") or "").strip()
    if not version:
        return _r("A4", "warn", "服务器未返回协议版本", "请检查服务器初始化响应")
    try:
        leaked, evidence = _probe_error_leakage(client)
    except Exception as exc:
        return _r("A4", "warn", f"协议版本：{version}；错误响应探测失败：{type(exc).__name__}", "请人工检查错误响应")
    if leaked:
        return _r(
            "A4", "fail",
            f"协议版本：{version}；错误响应泄露内部信息：{evidence}",
            "建议服务器统一错误响应格式，屏蔽堆栈与路径",
        )
    if evidence:
        return _r("A4", "pass", f"协议版本：{version}；错误响应：{evidence}", "错误响应未发现堆栈泄露")
    return _r("A4", "pass", f"协议版本：{version}；服务器未返回错误详情", "无处理建议")


def _detect_stack_trace(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _STACK_REGEX)


def _probe_error_leakage(client) -> tuple[bool, str]:
    """发送两条畸形请求；泄露判定用完整原文，展示证据做中心掩码。"""
    probes = [
        {"jsonrpc": "2.0", "id": 9001, "method": "__nonexistent_method__"},
        {"jsonrpc": "2.0", "id": 9002, "method": "tools/call", "params": {}},
    ]
    raw_parts: list[str] = []
    show_parts: list[str] = []
    for req in probes:
        method = req["method"]
        try:
            resp = client.send_raw(req)
        except Exception as exc:
            raw_parts.append(f"[{method}]发送异常：{exc}")
            show_parts.append(f"[{method}]发送异常：{P.mask_center(str(exc))}")
            continue
        err = resp.get("error") if isinstance(resp, dict) else None
        if not isinstance(err, dict):
            text = P.truncate(str(resp), 200)
            raw_parts.append(f"[{method}]响应：{text}")
            show_parts.append(f"[{method}]响应：{P.mask_center(text)}")
            continue
        code = err.get("code", "")
        msg = str(err.get("message", ""))
        data = str(err.get("data", ""))
        raw = f"[{method}] code={code} {msg}"
        shown = f"[{method}] code={code} {msg}"
        if data:
            raw += f" data={data}"
            shown += f" data={P.mask_center(data)}"
        raw_parts.append(raw)
        show_parts.append(shown)
    full = " | ".join(raw_parts)
    shown = P.truncate(" | ".join(show_parts), 400)
    if _detect_stack_trace(full):
        return True, f"命中泄露特征：{shown}"
    return False, shown


# ---------------------------------------------------------------------------
# B 组：工具元数据 / 响应
# ---------------------------------------------------------------------------
def _safe_list_tools(client):
    if client is None or not getattr(client, "connected", False):
        return None
    try:
        return list(client.list_tools())
    except Exception:
        return None


def check_metadata_injection(config, client=None) -> ItemResult:
    """B1：工具名称 / 描述 / 参数说明中的提示注入与混淆字符（静态，需工具清单）。"""
    tools = _safe_list_tools(client)
    if tools is None:
        return _r("B1", "skip", "未连接或无法获取工具列表", "连接服务器后就地复检")
    if not tools:
        return _r("B1", "skip", "服务器未提供工具", "无工具可检测")
    hits: list[str] = []
    urls: list[str] = []
    for tool in tools:
        if P.contains_prompt_injection(tool.name or ""):
            hits.append(f"工具名[{tool.name}]命中注入关键词")
        desc = tool.description or ""
        if P.contains_prompt_injection(desc):
            hits.append(f"工具[{tool.name}]描述命中注入关键词")
        if P.has_obfuscated_chars(desc):
            hits.append(f"工具[{tool.name}]描述含零宽或混淆字符")
        found = P.extract_urls(desc)
        if found:
            urls.append(f"工具[{tool.name}]描述含 URL：{P.truncate(found[0], 50)}")
        props = (tool.input_schema or {}).get("properties", {}) if isinstance(tool.input_schema, dict) else {}
        for pname, pdef in props.items():
            if not isinstance(pdef, dict):
                continue
            if P.contains_prompt_injection(pdef.get("description", "") or ""):
                hits.append(f"工具[{tool.name}]参数[{pname}]说明命中注入关键词")
    if hits:
        return _r("B1", "fail", _preview(hits), "建议清理工具名称、描述、参数说明中的指令性语句、混淆字符与外部链接")
    if urls:
        return _r("B1", "warn", _preview(urls), "含 URL 不一定是风险，请人工确认链接是否指向信任站点")
    return _r("B1", "pass", f"已检查 {len(tools)} 个工具，未发现可疑元数据", "保持工具描述简洁，避免指令性语句")


def check_result_injection(config, client=None) -> ItemResult:
    """B2：对只读工具做无害调用，检查响应是否含诱导内容（主动探测）。"""
    tools = _safe_list_tools(client)
    if tools is None:
        return _r("B2", "skip", "未连接或无法获取工具列表", "连接服务器后就地复检")
    if not tools:
        return _r("B2", "skip", "服务器未提供工具", "无工具可检测")
    readonly = [t for t in tools if _is_readonly(t)]
    if not readonly:
        return _r("B2", "skip", f"共有 {len(tools)} 个工具，但无只读工具可安全调用", "手动挑选工具进行测试")
    hits: list[str] = []
    errors: list[str] = []
    empty: list[str] = []
    called = 0
    total = min(len(readonly), MAX_RESULT_CALLS)
    skipped = len(readonly) - total
    for tool in readonly[:MAX_RESULT_CALLS]:
        args = _build_safe_args(tool.input_schema)
        try:
            resp = client.call_tool(tool.name, args)
            called += 1
        except Exception as exc:
            errors.append(f"{tool.name}：{str(exc)[:60]}")
            continue
        text = _extract_text(resp)
        if P.contains_prompt_injection(text):
            hits.append(f"工具[{tool.name}]响应命中注入关键词")
        found = P.extract_urls(text)
        if found:
            hits.append(f"工具[{tool.name}]响应含 URL：{P.truncate(found[0], 50)}")
        if not text.strip():
            empty.append(tool.name)
    if hits:
        return _r("B2", "fail", _preview(hits), "建议对工具响应文本进行内容过滤或标记可疑片段")
    if called == 0:
        detail = "；".join(errors[:3]) if errors else "无可用调用"
        return _r("B2", "warn", f"共 {total} 个只读工具均调用失败：{detail}", "请检查工具参数或服务器状态")
    if errors:
        evidence = f"成功调用 {called} 个，失败 {len(errors)} 个：{'；'.join(errors[:3])}"
        if skipped:
            evidence += f"；另有 {skipped} 个只读工具因数量上限未调用"
        return _r("B2", "warn", evidence, "请检查调用失败的工具参数或服务器状态")
    if empty and len(empty) == called:
        return _r("B2", "warn", f"已调用 {called} 个只读工具但响应内容均为空，无法判断", "请确认工具确有返回内容后再复测")
    extra = ""
    if empty:
        extra += f"；其中 {len(empty)} 个响应为空，仅供参考"
    if skipped:
        extra += f"；另有 {skipped} 个只读工具因数量上限未调用"
    return _r("B2", "pass", f"已安全调用 {called} 个只读工具，未发现响应注入{extra}", "保持响应内容规范")


def check_schema_constraints(config, client=None) -> ItemResult:
    """B3：input_schema 约束是否完整（静态，需工具清单）。"""
    tools = _safe_list_tools(client)
    if tools is None:
        return _r("B3", "skip", "未连接或无法获取工具列表", "连接服务器后就地复检")
    if not tools:
        return _r("B3", "skip", "服务器未提供工具", "无工具可检测")
    issues: list[str] = []
    for tool in tools:
        schema = tool.input_schema or {}
        if not isinstance(schema, dict):
            issues.append(f"工具[{tool.name}]input_schema 非字典")
            continue
        if schema.get("additionalProperties") is True:
            issues.append(f"工具[{tool.name}]允许额外属性")
        props = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
        if props and "required" not in schema:
            issues.append(f"工具[{tool.name}]声明了 {len(props)} 个属性但缺少 required 字段")
        for pname, pdef in props.items():
            if not isinstance(pdef, dict) or pdef.get("type") != "string":
                continue
            has_max = "maxLength" in pdef
            has_pattern = "pattern" in pdef
            has_enum = "enum" in pdef
            if not (has_max or has_pattern or has_enum):
                issues.append(f"工具[{tool.name}]参数[{pname}]字符串无任何约束")
            elif not has_max:
                issues.append(f"工具[{tool.name}]参数[{pname}]字符串缺少 maxLength")
    if not issues:
        return _r("B3", "pass", f"已检查 {len(tools)} 个工具的 Schema，约束完整", "保持参数约束规范")
    return _r("B3", "warn", _preview(issues), "建议为字符串参数添加 maxLength / pattern / enum，为对象添加 required")


def check_capability_consistency(config, client=None) -> ItemResult:
    """B4：能力声明（annotations）与实际名称 / 描述是否矛盾（静态，需工具清单）。"""
    tools = _safe_list_tools(client)
    if tools is None:
        return _r("B4", "skip", "未连接或无法获取工具列表", "连接服务器后就地复检")
    if not tools:
        return _r("B4", "skip", "服务器未提供工具", "无工具可检测")
    issues: list[str] = []
    for tool in tools:
        ann = tool.annotations or {}
        name_low = (tool.name or "").lower()
        desc_low = (tool.description or "").lower()
        read_only = ann.get("readOnlyHint")
        destructive = ann.get("destructiveHint")
        if P.as_bool(read_only):
            hit_name = [w for w in DANGEROUS_WORDS if w in name_low]
            hit_desc = [w for w in DANGEROUS_WORDS if w in desc_low]
            if hit_name:
                issues.append(f"工具[{tool.name}]声明只读但名称含危险词：{hit_name[0]}")
            if hit_desc:
                issues.append(f"工具[{tool.name}]声明只读但描述含危险词：{hit_desc[0]}")
        if destructive is not None and not P.as_bool(destructive):
            hit = [w for w in DELETE_WORDS if w in name_low or w in desc_low]
            if hit:
                issues.append(f"工具[{tool.name}]声明非破坏但含删除类词：{hit[0]}")
        if read_only is None and any(name_low.startswith(h) for h in READONLY_HINTS):
            issues.append(f"工具[{tool.name}]名称暗示只读但未声明 readOnlyHint")
    if not issues:
        return _r("B4", "pass", f"已检查 {len(tools)} 个工具，声明与命名一致", "保持能力声明与工具行为一致")
    return _r("B4", "fail", _preview(issues), "建议修正能力声明，或让名称 / 描述与实际行为一致")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _preview(items: list[str], limit: int = 5) -> str:
    text = "；".join(items[:limit])
    if len(items) > limit:
        text += f"；……共 {len(items)} 条"
    return text


def _is_readonly(tool) -> bool:
    ann = tool.annotations or {}
    if P.as_bool(ann.get("readOnlyHint")):
        return True
    if P.as_bool(ann.get("destructiveHint")):
        return False
    name_low = (tool.name or "").lower()
    return any(name_low.startswith(h) or f"_{h}" in name_low or f"-{h}" in name_low for h in READONLY_HINTS)


def _build_safe_args(schema) -> dict:
    if not isinstance(schema, dict):
        return {}
    required = schema.get("required", [])
    props = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
    args: dict = {}
    for key in required:
        if not isinstance(key, str):
            continue
        pdef = props.get(key, {})
        if not isinstance(pdef, dict):
            continue
        if "default" in pdef:
            args[key] = pdef["default"]
            continue
        ptype = pdef.get("type", "string")
        args[key] = {
            "string": "",
            "integer": 0,
            "number": 0.0,
            "boolean": False,
            "array": [],
            "object": {},
        }.get(ptype, "")
    return args


def _extract_text(resp) -> str:
    if not isinstance(resp, dict):
        return str(resp)
    parts = []
    content = resp.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
    if not parts:
        try:
            return json.dumps(resp, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(resp)
    return "\n".join(parts)


#: 注册表：{编号: (显示名, 检查函数)}。检查函数统一签名 (config, client) -> ItemResult。
CHECKS: dict[str, tuple[str, object]] = {
    "A1": (NAME["A1"], check_tls),
    "A2": (NAME["A2"], check_anonymous),
    "A3": (NAME["A3"], check_credentials),
    "A4": (NAME["A4"], check_handshake),
    "B1": (NAME["B1"], check_metadata_injection),
    "B2": (NAME["B2"], check_result_injection),
    "B3": (NAME["B3"], check_schema_constraints),
    "B4": (NAME["B4"], check_capability_consistency),
}