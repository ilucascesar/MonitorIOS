"""Cliente ONVIF mínimo (SOAP 1.2 + WS-UsernameToken digest) e WS-Discovery.

Implementado sem dependências externas: só os serviços necessários para descobrir perfis
e URLs RTSP (Device e Media v1), que todas as câmeras ONVIF Profile S suportam.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import logging
import os
import re
import socket
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape

log = logging.getLogger("cameras.onvif")

NS_DEVICE = "http://www.onvif.org/ver10/device/wsdl"
NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
NS_SCHEMA = "http://www.onvif.org/ver10/schema"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PW_DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
B64 = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"


class OnvifError(Exception):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el: ET.Element, *names: str) -> ET.Element | None:
    cur = el
    for n in names:
        nxt = None
        for child in cur.iter():
            if child is not cur and _local(child.tag) == n:
                nxt = child
                break
        if nxt is None:
            return None
        cur = nxt
    return cur


def _text(el: ET.Element | None, *names: str) -> str:
    if el is None:
        return ""
    t = _find(el, *names) if names else el
    return (t.text or "").strip() if t is not None else ""


class OnvifClient:
    def __init__(self, host: str, port: int = 80, username: str = "", password: str = "", timeout: float = 6):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout
        self.device_url = f"http://{host}:{port}/onvif/device_service"
        self.media_url = ""
        self.time_offset = dt.timedelta(0)

    # ------------------------------------------------------------- SOAP
    def _security_header(self) -> str:
        if not self.username:
            return ""
        nonce = os.urandom(16)
        created = (dt.datetime.now(dt.timezone.utc) + self.time_offset).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + self.password.encode("utf-8")).digest()).decode()
        return (
            f'<s:Header><wsse:Security xmlns:wsse="{WSSE}" xmlns:wsu="{WSU}" s:mustUnderstand="1"><wsse:UsernameToken>'
            f"<wsse:Username>{escape(self.username)}</wsse:Username>"
            f'<wsse:Password Type="{PW_DIGEST}">{digest}</wsse:Password>'
            f'<wsse:Nonce EncodingType="{B64}">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
            f"<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header>"
        )

    def _call(self, url: str, body: str, auth: bool = True) -> ET.Element:
        env = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="{NS_DEVICE}" '
            f'xmlns:trt="{NS_MEDIA}" xmlns:tt="{NS_SCHEMA}">'
            f"{self._security_header() if auth else ''}<s:Body>{body}</s:Body></s:Envelope>"
        ).encode("utf-8")
        req = urllib.request.Request(url, data=env, method="POST", headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        handlers = []
        if auth and self.username:
            # Algumas câmeras exigem HTTP Digest em vez de (ou além de) WS-Security.
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(None, url, self.username, self.password)
            handlers = [urllib.request.HTTPDigestAuthHandler(mgr), urllib.request.HTTPBasicAuthHandler(mgr)]
        opener = urllib.request.build_opener(*handlers)
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            payload = e.read() or b""
            fault = self._fault(payload)
            if e.code in (400, 401, 403, 500) and fault:
                raise OnvifError(fault) from None
            raise OnvifError(f"HTTP {e.code}") from None
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            raise OnvifError(f"sem conexão com {self.host}:{self.port} ({getattr(e, 'reason', e)})") from None
        try:
            root = ET.fromstring(payload)
        except ET.ParseError:
            raise OnvifError("resposta ONVIF inválida") from None
        fault = _find(root, "Fault")
        if fault is not None:
            raise OnvifError(self._fault(payload) or "SOAP fault")
        return root

    @staticmethod
    def _fault(payload: bytes) -> str:
        try:
            root = ET.fromstring(payload)
        except ET.ParseError:
            return ""
        f = _find(root, "Fault")
        if f is None:
            return ""
        reason = _text(f, "Reason", "Text") or _text(f, "faultstring")
        sub = " ".join(_local(t.text or "") for t in f.iter() if _local(t.tag) == "Value")
        msg = (reason or sub or "SOAP fault").strip()
        if re.search(r"NotAuthorized|not authorized|authority|Sender", sub + reason, re.I):
            msg = f"credenciais ONVIF recusadas ({msg})"
        return msg[:200]

    def _fix_host(self, url: str) -> str:
        """Câmeras às vezes anunciam IP interno/errado: mantém porta/caminho e usa o host configurado."""
        if not url:
            return url
        p = urlsplit(url)
        port = f":{p.port}" if p.port else ""
        return urlunsplit((p.scheme, f"{self.host}{port}", p.path, p.query, p.fragment))

    # -------------------------------------------------------- operations
    def sync_time(self) -> None:
        try:
            root = self._call(self.device_url, "<tds:GetSystemDateAndTime/>", auth=False)
        except OnvifError:
            return
        utc = _find(root, "UTCDateTime")
        if utc is None:
            return
        try:
            cam = dt.datetime(
                int(_text(utc, "Date", "Year")), int(_text(utc, "Date", "Month")), int(_text(utc, "Date", "Day")),
                int(_text(utc, "Time", "Hour")), int(_text(utc, "Time", "Minute")), int(_text(utc, "Time", "Second")),
                tzinfo=dt.timezone.utc,
            )
            self.time_offset = cam - dt.datetime.now(dt.timezone.utc)
        except ValueError:
            pass

    def device_information(self) -> dict:
        root = self._call(self.device_url, "<tds:GetDeviceInformation/>")
        return {k: _text(root, k) for k in ("Manufacturer", "Model", "FirmwareVersion", "SerialNumber", "HardwareId")}

    def _media_service(self) -> str:
        if self.media_url:
            return self.media_url
        try:
            root = self._call(self.device_url, "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>")
            x = _text(_find(root, "Media"), "XAddr")
        except OnvifError:
            x = ""
        self.media_url = self._fix_host(x) if x else self.device_url.replace("device_service", "media_service")
        return self.media_url

    def profiles(self) -> list[dict]:
        root = self._call(self._media_service(), "<trt:GetProfiles/>")
        out = []
        for prof in root.iter():
            if _local(prof.tag) != "Profiles":
                continue
            vec = _find(prof, "VideoEncoderConfiguration")
            out.append({
                "token": prof.get("token", ""),
                "name": _text(prof, "Name"),
                "encoding": _text(vec, "Encoding").lower() if vec is not None else "",
                "width": int(_text(vec, "Resolution", "Width") or 0) if vec is not None else 0,
                "height": int(_text(vec, "Resolution", "Height") or 0) if vec is not None else 0,
                "fps": float(_text(vec, "RateControl", "FrameRateLimit") or 0) if vec is not None else 0,
                "bitrate": int(_text(vec, "RateControl", "BitrateLimit") or 0) if vec is not None else 0,
            })
        return out

    def stream_uri(self, token: str) -> str:
        body = (
            "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
            f"<trt:ProfileToken>{escape(token)}</trt:ProfileToken></trt:GetStreamUri>"
        )
        root = self._call(self._media_service(), body)
        return self._fix_host(_text(root, "MediaUri", "Uri"))

    def snapshot_uri(self, token: str) -> str:
        try:
            root = self._call(self._media_service(), f"<trt:GetSnapshotUri><trt:ProfileToken>{escape(token)}</trt:ProfileToken></trt:GetSnapshotUri>")
        except OnvifError:
            return ""
        return self._fix_host(_text(root, "MediaUri", "Uri"))

    def probe(self) -> dict:
        """Resumo completo: dispositivo, perfis e URLs RTSP (sem credenciais)."""
        self.sync_time()
        info = {}
        try:
            info = self.device_information()
        except OnvifError as e:
            if "credenciais" in str(e):
                raise
        profiles = self.profiles()
        for p in profiles:
            try:
                p["rtsp_url"] = self.stream_uri(p["token"])
            except OnvifError as e:
                p["rtsp_url"] = ""
                p["error"] = str(e)
        return {"device": info, "profiles": profiles}


def discover(timeout: float = 3.0) -> list[dict]:
    """WS-Discovery: envia Probe multicast em todas as interfaces IPv4 locais."""
    msg_id = f"uuid:{uuid.uuid4()}"
    probe = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        f"<e:Header><w:MessageID>{msg_id}</w:MessageID>"
        "<w:To e:mustUnderstand=\"true\">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
        "<w:Action e:mustUnderstand=\"true\">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>"
        "</e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>"
    ).encode()
    try:
        import psutil

        local_ips = [
            a.address for addrs in psutil.net_if_addrs().values() for a in addrs
            if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.", "100."))
        ]
    except Exception:
        local_ips = ["0.0.0.0"]
    found: dict[str, dict] = {}
    socks = []
    for ip in local_ips or ["0.0.0.0"]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            s.bind((ip, 0))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            s.settimeout(0.3)
            s.sendto(probe, ("239.255.255.250", 3702))
            socks.append(s)
        except OSError as e:
            log.debug("discovery bind falhou em %s: %s", ip, e)
    import time as _t

    deadline = _t.time() + timeout
    while _t.time() < deadline and socks:
        for s in socks:
            try:
                data, addr = s.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            try:
                root = ET.fromstring(data)
            except ET.ParseError:
                continue
            xaddrs = _text(root, "XAddrs").split()
            scopes = _text(root, "Scopes").split()
            name = next((sc.rsplit("/", 1)[-1] for sc in scopes if "/name/" in sc), "")
            hw = next((sc.rsplit("/", 1)[-1] for sc in scopes if "/hardware/" in sc), "")
            port = 80
            for x in xaddrs:
                p = urlsplit(x)
                if p.hostname == addr[0]:
                    port = p.port or 80
                    break
            found[addr[0]] = {"host": addr[0], "onvif_port": port, "xaddrs": xaddrs, "name": name, "hardware": hw}
    for s in socks:
        s.close()
    return sorted(found.values(), key=lambda d: tuple(int(x) for x in d["host"].split(".")))
