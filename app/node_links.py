"""Local share-link import. Never sends node credentials to a converter."""
import base64
import json
import re
import copy
import yaml
from urllib.parse import unquote, urlsplit, parse_qs

PROTOCOLS = {"ss", "ssr", "socks5", "http", "vmess", "vless", "snell", "trojan", "hysteria",
             "hysteria2", "wireguard", "tuic", "ssh", "mieru", "anytls", "direct", "dns", "reject"}
ALIASES = {"hy2": "hysteria2", "hy": "hysteria", "wg": "wireguard", "socks": "socks5", "socks5h": "socks5",
           "http-proxy": "http", "https-proxy": "https"}
SCHEMES = PROTOCOLS | set(ALIASES) | {"https"}


def decode64(value):
    value = value.strip()
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")


def normalize_links(value):
    return value.strip().replace(r"\://", "://")


def is_node_source(value):
    value = normalize_links(value)
    return '|' in value or not re.fullmatch(r"https?://\S+", value) or ("@" in value and "#" in value)


def parse_clash_nodes(value):
    """Lossless import for every core protocol, including future protocol fields."""
    data = yaml.safe_load(value)
    nodes = data.get("proxies", [data]) if isinstance(data, dict) else data
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("请输入节点分享链接或 Clash YAML/JSON 节点配置")
    result = []
    for index, raw in enumerate(nodes, 1):
        if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
            raise ValueError(f"第 {index} 个节点缺少协议 type")
        node = copy.deepcopy(raw)
        node.setdefault("name", f"{node['type']}-{index}")
        special = node['type'] in ('direct', 'dns', 'reject')
        peers = node['type'] == 'wireguard' and node.get('peers')
        if not special and not peers and not node.get('server'):
            raise ValueError(f"第 {index} 个节点缺少 server")
        if not special and not peers and not (node.get('port') or node.get('port-range')):
            raise ValueError(f"第 {index} 个节点缺少 port / port-range")
        result.append(node)
    return result


def _transport(node, network, host="", path="", service=""):
    network = network or "tcp"
    if network not in ("tcp", "ws", "grpc", "http", "h2"):
        raise ValueError("不支持的节点传输类型")
    if network == "tcp":
        return
    node["network"] = network
    if network == "ws":
        node["ws-opts"] = {"path": path or "/", "headers": {"Host": host} if host else {}}
    elif network == "grpc":
        node["grpc-opts"] = {"grpc-service-name": service or path}
    elif network == "h2":
        node["h2-opts"] = {"host": [host] if host else [], "path": path or "/"}
    else:
        node["http-opts"] = {"path": [path or "/"], "headers": {"Host": [host]} if host else {}}


def parse_link(link):
    link = normalize_links(link)
    scheme, payload = link.split("://", 1)
    scheme = scheme.lower()
    scheme = ALIASES.get(scheme, scheme)
    link = scheme + "://" + payload
    if scheme not in SCHEMES:
        raise ValueError("不支持的节点协议")
    if scheme == "ssr":
        decoded = decode64(payload)
        main, _, query = decoded.partition('/?')
        server, port, protocol, cipher, obfs, password = main.rsplit(':', 5)
        q = {k: v[-1] for k, v in parse_qs(query).items()}
        node = {'type':'ssr', 'name':decode64(q['remarks']) if q.get('remarks') else 'ssr',
                'server':server.strip('[]'), 'port':int(port), 'protocol':protocol, 'cipher':cipher,
                'obfs':obfs, 'password':decode64(password), 'udp':True}
        for source, target in [('obfsparam','obfs-param'),('protoparam','protocol-param')]:
            if q.get(source): node[target]=decode64(q[source])
    elif scheme == "vmess":
        v = json.loads(decode64(payload.split("#", 1)[0]))
        node = {"name": v.get("ps") or "vmess", "type": "vmess", "server": v["add"],
                "port": int(v["port"]), "uuid": v["id"], "alterId": int(v.get("aid") or 0),
                "cipher": v.get("scy") or "auto", "tls": v.get("tls") == "tls", "udp": True}
        if node["tls"]:
            node["servername"] = v.get("sni") or v.get("host") or v["add"]
            if v.get("fp"): node["client-fingerprint"] = v["fp"]
        _transport(node, v.get("net"), v.get("host", ""), v.get("path", ""))
    else:
        if scheme == "ss" and "@" not in payload.split("#", 1)[0]:
            raw, sep, fragment = payload.partition("#")
            link = "ss://" + decode64(raw) + (sep + fragment if sep else "")
        u = urlsplit(link)
        q = {k: vals[-1] for k, vals in parse_qs(u.query, keep_blank_values=True).items()}
        common = {'sni','peer','fp','alpn','insecure','allowInsecure'}
        known = {
            'ss': {'plugin'}, 'vless': {'encryption','flow','security','pbk','sid','type','host','path','serviceName','headerType'},
            'trojan': {'type','host','path','serviceName','security','headerType'}, 'anytls': {'security'},
            'hysteria2': {'obfs','obfs-password'}, 'tuic': {'congestion_control','udp_relay_mode'},
            'hysteria': {'auth','protocol','upmbps','downmbps','up','down','obfs','obfsParam'},
            'snell': {'psk','version','obfs','obfs-host'},
            'wireguard': {'private-key','publickey','public-key','address','ip','reserved','mtu','presharedkey','pre-shared-key'},
            'mieru': {'transport','multiplexing'}, 'ssh': {'private-key'},
        }
        if set(q) - common - known.get(scheme, set()):
            raise ValueError('该链接含尚无映射的参数，请改用原生 Clash YAML/JSON 保留全部字段')
        if q.get('headerType') not in (None, '', 'none') or q.get('encryption') not in (None, '', 'none'):
            raise ValueError('该传输变体请使用 Clash YAML/JSON 节点配置')
        node = {"name": unquote(u.fragment) or scheme, "type": {"hy2": "hysteria2", "https": "http"}.get(scheme, scheme),
                "server": u.hostname, "port": u.port, "udp": True}
        user, password = unquote(u.username or ""), unquote(u.password or "")
        if scheme == "ss":
            credentials = (user + ":" + password) if u.password is not None else decode64(user)
            node["cipher"], node["password"] = credentials.split(":", 1)
            if q.get("plugin"):
                parts = q["plugin"].split(";")
                plugin = {"obfs-local": "obfs", "simple-obfs": "obfs"}.get(parts[0], parts[0])
                if plugin not in ("obfs", "v2ray-plugin"):
                    raise ValueError("不支持的 Shadowsocks 插件")
                opts = dict(part.split("=", 1) if "=" in part else (part, True) for part in parts[1:])
                node.update(plugin=plugin)
                node["plugin-opts"] = {({"obfs": "mode", "obfs-host": "host"}.get(k, k)): v for k, v in opts.items()}
        elif scheme == "vless":
            node["uuid"] = user
            if q.get("flow"): node["flow"] = q["flow"]
            security = q.get("security", "none")
            if security not in ("none", "tls", "reality"):
                raise ValueError("不支持的 VLESS 安全类型")
            node["tls"] = security in ("tls", "reality")
            if security == "reality":
                if not q.get("pbk"): raise ValueError("Reality 缺少公钥")
                node["reality-opts"] = {"public-key": q["pbk"], "short-id": q.get("sid", "")}
                node["client-fingerprint"] = q.get("fp") or "chrome"
            _transport(node, q.get("type"), q.get("host", ""), q.get("path", ""), q.get("serviceName", ""))
        elif scheme in ("trojan", "anytls"):
            node["password"] = user + (":" + password if u.password is not None else "")
            if scheme == 'trojan':
                _transport(node, q.get("type"), q.get("host", ""), q.get("path", ""), q.get("serviceName", ""))
            elif q.get('security') == 'reality':
                raise ValueError('Mihomo AnyTLS 不支持 Reality')
        elif scheme in ("hysteria2", "hy2"):
            node["password"] = user + (":" + password if u.password is not None else "")
            if q.get("obfs"):
                node.update(obfs=q["obfs"])
                node["obfs-password"] = q.get("obfs-password", "")
        elif scheme == "tuic":
            node.update(uuid=user, password=password)
            for key in ("congestion_control", "udp_relay_mode"):
                if q.get(key): node[key.replace("_", "-")] = q[key]
        elif scheme == "hysteria":
            node['auth-str'] = q.get('auth') or user
            node['protocol'] = q.get('protocol', 'udp')
            node['up'] = q.get('upmbps') or q.get('up') or '10'
            node['down'] = q.get('downmbps') or q.get('down') or '50'
            if q.get('obfsParam'): node['obfs'] = q['obfsParam']
            if q.get('peer'): node['sni'] = q['peer']
        elif scheme == "snell":
            node['psk'] = user or q.get('psk', '')
            node['version'] = int(q.get('version', '3'))
            if q.get('obfs'):
                node['obfs-opts'] = {'mode':q['obfs'], 'host':q.get('obfs-host', '')}
        elif scheme == "wireguard":
            node['private-key'] = user or q.get('private-key', '')
            node['public-key'] = q.get('publickey') or q.get('public-key', '')
            addresses = (q.get('address') or q.get('ip') or '').split(',')
            for address in addresses:
                if address: node['ipv6' if ':' in address else 'ip'] = address.split('/')[0]
            if q.get('reserved'): node['reserved'] = [int(x) for x in q['reserved'].split(',')]
            if q.get('mtu'): node['mtu'] = int(q['mtu'])
            if q.get('presharedkey') or q.get('pre-shared-key'):
                node['pre-shared-key'] = q.get('presharedkey') or q['pre-shared-key']
        elif scheme == 'mieru':
            node.update(username=user, password=password, transport=q.get('transport', 'TCP').upper())
            if q.get('multiplexing'): node['multiplexing'] = q['multiplexing']
        elif scheme == 'ssh':
            node.update(username=user, password=password)
            if q.get('private-key'): node['private-key'] = q['private-key']
        else:
            if user: node.update(username=user, password=password)
            if scheme == "https": node["tls"] = True
        if q.get("sni") or q.get("peer"):
            node["servername" if scheme == "vless" else "sni"] = q.get("sni") or q["peer"]
        if q.get("fp"): node["client-fingerprint"] = q["fp"]
        if q.get('peer') and scheme == 'hysteria': node['sni'] = q['peer']
        if q.get("alpn"): node["alpn"] = q["alpn"].split(",")
        if q.get("insecure") in ("1", "true") or q.get("allowInsecure") in ("1", "true"):
            node["skip-cert-verify"] = True
    if not node.get("server") or not isinstance(node.get("port"), int) or not 1 <= node["port"] <= 65535:
        raise ValueError("节点缺少合法的服务器地址或端口")
    if node["type"] in ("vless", "vmess", "tuic") and not node.get("uuid"):
        raise ValueError("节点缺少 UUID")
    return node


def parse_node_source(value):
    value = normalize_links(value)
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
        try:
            return parse_clash_nodes(value)
        except (ValueError, yaml.YAMLError):
            raise ValueError("无效的 Clash YAML/JSON 节点配置，请检查 type、server 和 port 等必填项") from None
    links = [s.strip() for s in re.split(r"[|\r\n]+", normalize_links(value)) if s.strip()]
    if not links:
        raise ValueError("节点链接为空")
    result = []
    for index, link in enumerate(links, 1):
        try:
            result.append(parse_link(link))
        except Exception:
            # Credentials and full URIs must not leak via parser exceptions.
            raise ValueError(f"第 {index} 个节点链接无效或使用了不支持的传输参数") from None
    return result


def validate_source(value):
    value = normalize_links(value)
    if not is_node_source(value):
        return value
    parse_node_source(value)
    return value
