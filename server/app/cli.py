"""Linha de comando de administração.

Uso:  python -m app.cli <comando> [opções]      (dentro de server/, com o venv ativo)
Senhas são sempre pedidas de forma interativa (getpass) e nunca aceitas como argumento.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import urllib.request
from pathlib import Path


def _rt(workers: bool = False):
    from .runtime import Runtime

    return Runtime(Path(os.environ["MONITORIOS_DATA"]) if os.environ.get("MONITORIOS_DATA") else None, start_workers=workers)


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _ask_password(prompt: str = "Senha: ", confirm: bool = True) -> str:
    pw = getpass.getpass(prompt)
    if confirm and getpass.getpass("Confirme: ") != pw:
        sys.exit("As senhas não conferem.")
    return pw


def _server_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=2):
            return True
    except Exception:
        return False


def cmd_serve(a) -> None:
    from .server import ServerHandle

    ServerHandle().run()


def cmd_init_admin(a) -> None:
    rt = _rt()
    if rt.auth.has_users() and not a.force:
        sys.exit("Já existem usuários. Use 'add-user' ou --force.")
    uid = rt.auth.create_user(a.username, _ask_password(), "admin")
    print(f"Administrador '{a.username}' criado (id {uid}).")


def cmd_add_user(a) -> None:
    rt = _rt()
    uid = rt.auth.create_user(a.username, _ask_password(), a.role)
    print(f"Usuário '{a.username}' criado (id {uid}, papel {a.role}).")


def cmd_passwd(a) -> None:
    _rt().auth.set_password(a.username, _ask_password("Nova senha: "))
    print("Senha alterada; sessões anteriores foram encerradas.")


def cmd_set_smb(a) -> None:
    rt = _rt()
    changes = {}
    if a.share:
        changes["smb_share"] = a.share
    if a.root:
        changes["storage_root"] = a.root
    if changes:
        rt.cfg.update(changes)
    rt.storage.set_smb_credentials(a.username, _ask_password("Senha do compartilhamento SMB: ", confirm=False))
    print("Credenciais SMB salvas (cifradas com DPAPI). Testando...")
    _print(rt.storage.check_health())


def cmd_smb_test(a) -> None:
    rt = _rt()
    st = rt.storage.check_health()
    _print(st)
    if st["status"] == "unavailable":
        sys.exit(1)


def cmd_import_agentdvr(a) -> None:
    from .agentdvr_import import import_cameras

    _print(import_cameras(_rt(), Path(a.objects)))


def cmd_cameras(a) -> None:
    rt = _rt()
    _print([rt.cameras.public(c) for c in rt.db.list_cameras()])


def cmd_test_camera(a) -> None:
    rt = _rt()
    cam = rt.db.get_camera(a.id)
    if not cam:
        sys.exit("câmera não encontrada")
    _print(rt.cameras.test_camera(cam))


def cmd_discover(a) -> None:
    from .cameras.onvif import discover

    _print(discover(a.timeout))


def cmd_onvif_probe(a) -> None:
    rt = _rt()
    pw = _ask_password("Senha ONVIF: ", confirm=False) if a.username else ""
    _print(rt.cameras.onvif_probe(a.host, a.port, a.username or "", pw))


def cmd_record_test(a) -> None:
    """Teste real: grava uma câmera por N segundos, indexa e envia ao SMB."""
    rt = _rt(workers=False)
    if _server_running(rt.cfg.settings.http_port):
        sys.exit("O serviço está rodando; pare-o antes (ele já está gravando).")
    from .logging_setup import setup_logging

    setup_logging(rt.cfg.logs_dir, "INFO", console=True)
    cams = [c for c in rt.db.list_cameras() if not a.id or c["id"] == a.id]
    if not cams:
        sys.exit("nenhuma câmera")
    rt.supervisor.only_cameras = {c["id"] for c in cams}
    rt.storage.check_health()
    rt.storage.start()
    rt.supervisor.start()
    t_end = time.time() + a.seconds
    try:
        while time.time() < t_end:
            time.sleep(5)
            st = rt.supervisor.status()
            print(json.dumps({k: {"state": v["state"], "error": v["last_error"][:120]} for k, v in st.items()}, ensure_ascii=False))
    finally:
        rt.supervisor.stop()
        time.sleep(3)
        deadline = time.time() + 60
        while time.time() < deadline and rt.db.usage_bytes("local") and rt.storage.state.get("status") == "ok":
            time.sleep(2)
        rt.storage.stop()
    for c in cams:
        segs = rt.db.query("SELECT id, location, path, start_ts, end_ts, duration, size, codec, width, height, status FROM segments WHERE camera_id=? ORDER BY id DESC LIMIT 10", (c["id"],))
        print(f"\n== {c['name']} ==")
        _print(segs)


def cmd_reconcile(a) -> None:
    rt = _rt()
    rt.storage.check_health()
    _print({"buffer": rt.supervisor.reconcile_buffer(), **rt.storage.reconcile_remote()})


def cmd_retention(a) -> None:
    rt = _rt()
    rt.storage.check_health()
    _print(rt.storage.run_retention())


def cmd_settings(a) -> None:
    rt = _rt()
    if a.set:
        changes = {}
        for item in a.set:
            k, _, v = item.partition("=")
            try:
                changes[k] = json.loads(v)
            except json.JSONDecodeError:
                changes[k] = v
        rt.cfg.update(changes)
    _print(rt.cfg.settings.model_dump())


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="monitorios", description="Administração do servidor MonitorIOS")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="executa o servidor em primeiro plano").set_defaults(fn=cmd_serve)
    s = sub.add_parser("init-admin", help="cria o primeiro administrador")
    s.add_argument("--username", default="admin")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init_admin)
    s = sub.add_parser("add-user")
    s.add_argument("username")
    s.add_argument("--role", choices=["viewer", "admin"], default="viewer")
    s.set_defaults(fn=cmd_add_user)
    s = sub.add_parser("passwd")
    s.add_argument("username")
    s.set_defaults(fn=cmd_passwd)
    s = sub.add_parser("set-smb", help="define usuário/senha do compartilhamento do roteador")
    s.add_argument("--username", required=True)
    s.add_argument("--share")
    s.add_argument("--root")
    s.set_defaults(fn=cmd_set_smb)
    sub.add_parser("smb-test", help="teste real de leitura/escrita no armazenamento").set_defaults(fn=cmd_smb_test)
    s = sub.add_parser("import-agentdvr", help="importa câmeras do Agent DVR (somente leitura)")
    s.add_argument("--objects", default=r"C:\Program Files\Agent\Media\XML\objects.json")
    s.set_defaults(fn=cmd_import_agentdvr)
    sub.add_parser("cameras").set_defaults(fn=cmd_cameras)
    s = sub.add_parser("test-camera")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_test_camera)
    s = sub.add_parser("discover", help="descoberta ONVIF (WS-Discovery)")
    s.add_argument("--timeout", type=float, default=3)
    s.set_defaults(fn=cmd_discover)
    s = sub.add_parser("onvif-probe")
    s.add_argument("host")
    s.add_argument("--port", type=int, default=80)
    s.add_argument("--username")
    s.set_defaults(fn=cmd_onvif_probe)
    s = sub.add_parser("record-test", help="grava por N segundos e mostra os segmentos indexados")
    s.add_argument("--id", type=int)
    s.add_argument("--seconds", type=int, default=150)
    s.set_defaults(fn=cmd_record_test)
    sub.add_parser("reconcile").set_defaults(fn=cmd_reconcile)
    sub.add_parser("retention").set_defaults(fn=cmd_retention)
    s = sub.add_parser("settings", help="mostra/altera settings.json (ex.: --set retention_days=15)")
    s.add_argument("--set", nargs="*")
    s.set_defaults(fn=cmd_settings)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
