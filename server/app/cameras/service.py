"""Cadastro de câmeras, testes de conexão e verificação periódica de alcance."""
from __future__ import annotations

import logging
import socket
import threading
import time
from urllib.parse import urlsplit

from ..config import Config
from ..database import Database
from ..recordings.indexer import probe_stream
from ..security import secrets as vault
from ..security.paths import sanitize_folder
from . import netfind
from .onvif import OnvifClient, OnvifError
from .urls import build_stream_url, strip_credentials, with_credentials

log = logging.getLogger("cameras.service")

EDITABLE = {
    "name", "host", "onvif_port", "rtsp_port", "username", "password", "main_url", "sub_url", "record_stream",
    "audio_mode", "record_enabled", "live_enabled", "sort_order", "codec", "width", "height", "fps",
    "sub_codec", "sub_width", "sub_height",
}


def tcp_check(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class CameraService:
    def __init__(self, cfg: Config, db: Database, events):
        self.cfg = cfg
        self.db = db
        self.events = events
        self.reachable: dict[int, dict] = {}
        # Seguir troca de IP pelo MAC (substituíveis nos testes).
        self.mac_of = netfind.mac_of
        self.find_host = netfind.find_host
        self.on_change = None  # runtime.apply_camera_changes
        self._relocated_at: dict[int, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ CRUD
    def public(self, cam: dict) -> dict:
        """Representação sem segredos (para API/app)."""
        return {
            "id": cam["id"], "name": cam["name"], "host": cam["host"], "onvif_port": cam["onvif_port"],
            "rtsp_port": cam["rtsp_port"], "username": cam["username"], "has_password": bool(cam["password_enc"]),
            "main_url": strip_credentials(cam["main_url"]), "sub_url": strip_credentials(cam["sub_url"]),
            "record_stream": cam["record_stream"], "audio_mode": cam["audio_mode"],
            "record_enabled": bool(cam["record_enabled"]), "live_enabled": bool(cam["live_enabled"]),
            "folder": cam["folder"], "codec": cam["codec"], "width": cam["width"], "height": cam["height"],
            "fps": cam["fps"], "sub_codec": cam["sub_codec"], "sub_width": cam["sub_width"],
            "sub_height": cam["sub_height"], "sort_order": cam["sort_order"],
        }

    def _validate(self, data: dict) -> dict:
        out = {k: v for k, v in data.items() if k in EDITABLE and v is not None}
        if "name" in out:
            out["name"] = str(out["name"]).strip()[:64]
            if not out["name"]:
                raise ValueError("nome obrigatório")
        if "host" in out:
            out["host"] = str(out["host"]).strip()
            if not out["host"] or any(c in out["host"] for c in "/@ \\"):
                raise ValueError("host inválido")
        for k in ("main_url", "sub_url"):
            if k in out and out[k]:
                u = str(out[k]).strip()
                p = urlsplit(u)
                if p.scheme.lower() not in ("rtsp", "rtsps") or not p.hostname:
                    raise ValueError(f"{k} deve ser uma URL rtsp://")
                if p.username and not out.get("username"):
                    out["username"] = p.username
                if p.password and "password" not in out:
                    out["password"] = p.password
                out[k] = strip_credentials(u)
        for k in ("onvif_port", "rtsp_port"):
            if k in out and not (1 <= int(out[k]) <= 65535):
                raise ValueError(f"{k} inválida")
        if "record_stream" in out and out["record_stream"] not in ("main", "sub"):
            raise ValueError("record_stream deve ser main ou sub")
        if "audio_mode" in out and out["audio_mode"] not in ("none", "aac", "copy"):
            raise ValueError("audio_mode deve ser none, aac ou copy")
        for k in ("record_enabled", "live_enabled"):
            if k in out:
                out[k] = int(bool(out[k]))
        if "password" in out:
            out["password_enc"] = vault.encrypt(str(out.pop("password")))
        return out

    def _unique_folder(self, name: str) -> str:
        base = sanitize_folder(name)
        used = {c["folder"].lower() for c in self.db.list_cameras(include_deleted=True)}
        folder, i = base, 2
        while folder.lower() in used:
            folder = f"{base}_{i}"
            i += 1
        return folder

    def create(self, data: dict) -> dict:
        clean = self._validate(data)
        if "name" not in clean or "host" not in clean:
            raise ValueError("nome e host são obrigatórios")
        clean["folder"] = self._unique_folder(data.get("folder") or clean["name"])
        cam_id = self.db.insert_camera(clean)
        self.events.emit("info", "camera", f"câmera cadastrada: {clean['name']}", cam_id)
        return self.public(self.db.get_camera(cam_id))

    def update(self, cam_id: int, data: dict) -> dict:
        if not self.db.get_camera(cam_id):
            raise KeyError(cam_id)
        clean = self._validate(data)
        if "host" in clean and clean["host"] != self.db.get_camera(cam_id)["host"]:
            clean["mac"] = ""  # outro aparelho: o MAC é reaprendido
        if any(k in clean for k in ("main_url", "sub_url")):
            # Nova URL: codec será redetectado no primeiro segmento.
            if "main_url" in clean and "codec" not in clean:
                clean["codec"] = ""
        self.db.update_camera(cam_id, clean)
        return self.public(self.db.get_camera(cam_id))

    def delete(self, cam_id: int) -> None:
        cam = self.db.get_camera(cam_id)
        if not cam:
            raise KeyError(cam_id)
        self.db.soft_delete_camera(cam_id)
        self.events.emit("info", "camera", f"câmera removida: {cam['name']} (gravações mantidas até a retenção)", cam_id)

    # ----------------------------------------------------------- testing
    def test_camera(self, cam: dict) -> dict:
        """Testa ONVIF (se houver porta) e os fluxos RTSP; atualiza codec/resolução detectados."""
        password = vault.decrypt(cam["password_enc"]) if cam.get("password_enc") else ""
        result: dict = {"tcp_onvif": tcp_check(cam["host"], cam["onvif_port"]), "tcp_rtsp": tcp_check(cam["host"], cam["rtsp_port"])}
        try:
            result["onvif"] = OnvifClient(cam["host"], cam["onvif_port"], cam["username"], password).probe()
        except OnvifError as e:
            result["onvif_error"] = str(e)
        updates = {}
        for which in ("main", "sub"):
            url = build_stream_url(cam, which) if (cam.get(f"{which}_url")) else ""
            if not url:
                continue
            info = probe_stream(self.cfg.ffprobe, url)
            result[f"rtsp_{which}"] = info
            if info.get("ok"):
                if which == "main":
                    updates.update(codec=info["codec"], width=info["width"], height=info["height"], fps=info["fps"])
                else:
                    updates.update(sub_codec=info["codec"], sub_width=info["width"], sub_height=info["height"])
        if updates:
            self.db.update_camera(cam["id"], updates)
        return result

    def onvif_probe(self, host: str, port: int, username: str, password: str) -> dict:
        client = OnvifClient(host, port, username, password)
        data = client.probe()
        for p in data["profiles"]:
            p["rtsp_url"] = strip_credentials(p.get("rtsp_url", ""))
        return data

    def probe_rtsp(self, url: str, username: str, password: str) -> dict:
        return probe_stream(self.cfg.ffprobe, with_credentials(strip_credentials(url), username, password) if username else url)

    # ------------------------------------------------------- reachability
    def start_health(self, interval: float = 30) -> None:
        self._thread = threading.Thread(target=self._health_loop, args=(interval,), name="cam-health", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # Falhas seguidas antes de considerar a câmera fora da rede: câmeras baratas às vezes recusam
    # uma conexão nova enquanto já servem o fluxo da gravação, e uma falha isolada geraria alarme falso.
    UNREACHABLE_AFTER = 3

    def _health_loop(self, interval: float) -> None:
        while not self._stop.is_set():
            for cam in self.db.list_cameras():
                self.check_reachability(cam)
            self._stop.wait(interval)

    def check_reachability(self, cam: dict, probe=None) -> bool:
        port = cam["rtsp_port"]
        if cam["main_url"]:
            port = urlsplit(cam["main_url"]).port or 554
        probe = probe or (lambda: tcp_check(cam["host"], port, 3.0) or tcp_check(cam["host"], cam["onvif_port"], 3.0))
        ok = probe()
        st = self.reachable.get(cam["id"], {})
        prev = st.get("reachable")
        failures = 0 if ok else st.get("failures", 0) + 1
        reachable = True if ok else (False if prev is None or failures >= self.UNREACHABLE_AFTER else prev)
        self.reachable[cam["id"]] = {"reachable": reachable, "failures": failures, "checked_at": time.time()}
        if prev is not None and prev != reachable:
            self.events.emit("warning" if not reachable else "info", "camera",
                             f"{cam['name']}: {'voltou a responder' if reachable else 'não responde na rede'}", cam["id"])
        if "mac" in cam:
            self._follow_mac(cam, ok, port)
        return reachable

    # Intervalo mínimo entre varreduras da rede por câmera.
    RELOCATE_EVERY = 300

    def _follow_mac(self, cam: dict, ok: bool, port: int) -> None:
        """O roteador pode dar o IP de uma câmera para outra: o MAC diz qual aparelho responde."""
        current = self.mac_of(cam["host"]) if ok else ""
        if not cam["mac"]:
            if current:
                self.db.update_camera(cam["id"], {"mac": current})
            return
        if current == cam["mac"]:
            return
        now = time.time()
        if now - self._relocated_at.get(cam["id"], 0) < self.RELOCATE_EVERY:
            return
        self._relocated_at[cam["id"]] = now
        new = self.find_host(cam["mac"], cam["host"], port, tcp_check, self.mac_of)
        if not new or new == cam["host"]:
            return
        old = cam["host"]
        self.db.update_camera(cam["id"], {
            "host": new,
            "main_url": netfind.with_host(cam["main_url"], old, new),
            "sub_url": netfind.with_host(cam["sub_url"], old, new),
        })
        log.info("câmera mudou de IP", extra={"ctx": {"camera": cam["id"], "from": old, "to": new}})
        self.events.emit("warning", "camera", f"{cam['name']} mudou de IP ({old} → {new}); configuração atualizada", cam["id"])
        if self.on_change:
            self.on_change(cam["id"])
