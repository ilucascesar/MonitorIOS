"""Serviço do Windows "MonitorIOS".

Executado pelo SCM como:  "<python.exe base>" "<este arquivo>"
(sem pythonservice.exe, o que evita os problemas conhecidos de pywin32 com venv).
As dependências vêm do venv em server/.venv, adicionado ao sys.path aqui.

Gerenciado pelos scripts install.ps1 / service.ps1 / uninstall.ps1.
"""
from __future__ import annotations

import os
import site
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER_DIR = HERE.parent
VENV_SITE = SERVER_DIR / ".venv" / "Lib" / "site-packages"
if VENV_SITE.is_dir():
    site.addsitedir(str(VENV_SITE))
sys.path.insert(0, str(SERVER_DIR))

import servicemanager  # noqa: E402
import win32service  # noqa: E402
import win32serviceutil  # noqa: E402

SERVICE_ACCEPT_PRESHUTDOWN = 0x100
SERVICE_CONTROL_PRESHUTDOWN = 0x0F


def _base_python() -> str:
    exe = getattr(sys, "_base_executable", None) or sys.executable
    return exe.replace("pythonw.exe", "python.exe")


class MonitorIOSService(win32serviceutil.ServiceFramework):
    _svc_name_ = "MonitorIOS"
    _svc_display_name_ = "MonitorIOS - Gravação de câmeras"
    _svc_description_ = "Gravação contínua das câmeras IP, índice das gravações e API para o app iOS."
    _exe_name_ = _base_python()
    _exe_args_ = f'"{Path(__file__).resolve()}"'

    def __init__(self, args):
        super().__init__(args)
        self.handle = None
        self._stopping = threading.Event()

    def GetAcceptedControls(self):
        # PRESHUTDOWN dá até ~3 min no desligamento do Windows para fechar os segmentos.
        return super().GetAcceptedControls() | SERVICE_ACCEPT_PRESHUTDOWN

    def SvcStop(self):
        self._begin_stop()

    def SvcShutdown(self):
        self._begin_stop()

    def SvcOtherEx(self, control, event_type, data):
        if control == SERVICE_CONTROL_PRESHUTDOWN:
            self._begin_stop()
            return 0
        return super().SvcOtherEx(control, event_type, data)

    def _begin_stop(self):
        if self._stopping.is_set():
            return
        self._stopping.set()
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=60000)
        if self.handle:
            self.handle.stop()

    def SvcDoRun(self):
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STARTED, (self._svc_name_, ""))
        from app.server import ServerHandle

        try:
            self.handle = ServerHandle(console=False)
        except Exception as e:  # configuração inválida etc.
            servicemanager.LogErrorMsg(f"MonitorIOS não iniciou: {e!r}")
            raise
        worker = threading.Thread(target=self.handle.run, name="server", daemon=True)
        worker.start()
        while worker.is_alive():
            worker.join(5)
            if self._stopping.is_set():
                # Mantém o SCM informado enquanto os FFmpeg finalizam os segmentos.
                self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=30000)
        if not self._stopping.is_set():
            # Servidor caiu sozinho: sai com erro para o SCM aplicar a política de reinício.
            servicemanager.LogErrorMsg("MonitorIOS: servidor HTTP encerrou inesperadamente")
            os._exit(1)
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STOPPED, (self._svc_name_, ""))


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # Iniciado pelo SCM.
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(MonitorIOSService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        # install / remove / start / stop / restart / debug
        win32serviceutil.HandleCommandLine(MonitorIOSService)
