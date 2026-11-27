#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Ponte go2rtc (câmeras iCSee/XM sem RTSP)

Câmeras iCSee/XMeye (principalmente as solares a bateria) NÃO têm
servidor RTSP: falam o protocolo proprietário DVRIP na porta 34567.
O go2rtc fala DVRIP e republica o vídeo como RTSP local.

Fluxo:
    câmera --DVRIP:34567--> go2rtc --RTSP 127.0.0.1:8554--> RRFS (OpenCV)

Este módulo:
  1. localiza (ou baixa) o binário do go2rtc;
  2. sobe o go2rtc como subprocesso (ou reaproveita um já rodando);
  3. registra cada câmera DVRIP via API HTTP (127.0.0.1:1984);
  4. vigia o go2rtc e registra de novo se ele reiniciar.

Câmeras RTSP comuns NÃO passam por aqui (continuam iguais).
"""

import os
import sys
import json
import atexit
import shutil
import logging
import platform
import threading
import subprocess
import time
import urllib.request
import urllib.error
from urllib.parse import quote

logger = logging.getLogger('RRFS.go2rtc')

PROTOCOLO_DVRIP  = 'dvrip'
PORTA_DVRIP      = 34567
API_HOST         = '127.0.0.1'
API_PORTA        = 1984
RTSP_PORTA       = 8554

if '__compiled__' in globals():
    _BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
else:
    _BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DADOS_DIR = os.environ.get('RRFS_DADOS_DIR', os.path.join(_BASE_DIR, 'dados'))
BIN_DIR   = os.path.join(DADOS_DIR, 'bin')
CFG_PATH  = os.path.join(DADOS_DIR, 'config', 'go2rtc.yaml')
LOG_PATH  = os.path.join(DADOS_DIR, 'logs', 'go2rtc.log')

_lock       = threading.Lock()
_processo   = None
_streams    = {}      # nome -> src dvrip (para re-registrar)
_vigia_ok   = False


# ── Helpers de nome/URL ───────────────────────────────────────────────────
def nome_stream(cam_id: int) -> str:
    return f"rrfs_cam_{cam_id}"


def url_rtsp_local(cam_id: int) -> str:
    """URL RTSP (local) que o RRFS usa para ler a câmera DVRIP."""
    return f"rtsp://{API_HOST}:{RTSP_PORTA}/{nome_stream(cam_id)}"


def _src_dvrip(ip, porta, usuario, senha) -> str:
    # safe='' codifica TUDO (%, @, #, :...) — a senha vai exatamente
    # como foi digitada, sem precisar escrever %25 na tela.
    u = quote(usuario or '', safe='')
    s = quote(senha or '',   safe='')
    auth = f"{u}:{s}@" if (u and s) else (f"{u}@" if u else "")
    try:
        p = int(porta) if porta else PORTA_DVRIP
    except Exception:
        p = PORTA_DVRIP
    return f"dvrip://{auth}{ip}:{p}?channel=0&subtype=0"


# ── API do go2rtc ─────────────────────────────────────────────────────────
def _api(metodo: str, caminho: str, timeout: float = 3.0):
    req = urllib.request.Request(
        f"http://{API_HOST}:{API_PORTA}{caminho}", method=metodo
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def go2rtc_no_ar() -> bool:
    try:
        _api('GET', '/api')
        return True
    except Exception:
        return False


# ── Binário ───────────────────────────────────────────────────────────────
def _arquitetura() -> str:
    m = platform.machine().lower()
    if m in ('x86_64', 'amd64'):
        return 'amd64'
    if m in ('aarch64', 'arm64'):
        return 'arm64'
    if m.startswith('arm'):
        return 'arm'
    return 'amd64'


def _localizar_binario() -> str:
    candidatos = [
        os.environ.get('RRFS_GO2RTC_BIN', ''),
        os.path.join(BIN_DIR, 'go2rtc'),
        shutil.which('go2rtc') or '',
        os.path.join(_BASE_DIR, 'go2rtc'),
        os.path.join(_BASE_DIR, 'go2rtc_linux_amd64'),
    ]
    for c in candidatos:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return ''


def _baixar_binario() -> str:
    os.makedirs(BIN_DIR, exist_ok=True)
    destino = os.path.join(BIN_DIR, 'go2rtc')
    url = ("https://github.com/AlexxIT/go2rtc/releases/latest/download/"
           f"go2rtc_linux_{_arquitetura()}")
    tmp = destino + '.tmp'
    try:
        logger.info(f"⬇️  Baixando go2rtc: {url}")
        req = urllib.request.Request(url, headers={'User-Agent': 'RRFS-Cameras/1.0'})
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, 'wb') as f:
            shutil.copyfileobj(r, f)
        if os.path.getsize(tmp) < 1024 * 1024:
            raise IOError("arquivo baixado menor que o esperado")
        os.replace(tmp, destino)
        os.chmod(destino, 0o755)
        logger.info(f"✓ go2rtc instalado em {destino}")
        return destino
    except Exception as e:
        logger.error(f"✕ Não foi possível baixar o go2rtc: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return ''


# ── Processo ──────────────────────────────────────────────────────────────
def _escrever_config():
    os.makedirs(os.path.dirname(CFG_PATH), exist_ok=True)
    with open(CFG_PATH, 'w', encoding='utf-8') as f:
        f.write(
            "# Gerado automaticamente pelo RRFS Cameras\n"
            "api:\n"
            f"  listen: \"{API_HOST}:{API_PORTA}\"\n"
            "rtsp:\n"
            f"  listen: \"{API_HOST}:{RTSP_PORTA}\"\n"
            "webrtc:\n"
            "  listen: \"\"\n"
        )


def _encerrar_processo():
    global _processo
    if _processo and _processo.poll() is None:
        try:
            _processo.terminate()
            _processo.wait(timeout=5)
        except Exception:
            try:
                _processo.kill()
            except Exception:
                pass
    _processo = None


atexit.register(_encerrar_processo)


def garantir_go2rtc() -> bool:
    """Garante que o go2rtc está rodando. Retorna True se a API responde."""
    global _processo
    with _lock:
        if go2rtc_no_ar():
            return True

        binario = _localizar_binario() or _baixar_binario()
        if not binario:
            return False

        _escrever_config()
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        try:
            logf = open(LOG_PATH, 'ab')
            _processo = subprocess.Popen(
                [binario, '-config', CFG_PATH],
                stdout=logf, stderr=subprocess.STDOUT,
                cwd=os.path.dirname(CFG_PATH),
            )
            logger.info(f"go2rtc iniciado (pid {_processo.pid})")
        except Exception as e:
            logger.error(f"Falha ao iniciar go2rtc: {e}")
            return False

    for _ in range(30):
        if go2rtc_no_ar():
            return True
        time.sleep(0.2)
    logger.error(f"go2rtc não respondeu na API. Veja {LOG_PATH}")
    return False


def _registrar_api(nome: str, src: str) -> bool:
    try:
        _api('PUT', f"/api/streams?name={quote(nome, safe='')}"
                    f"&src={quote(src, safe='')}")
        return True
    except urllib.error.HTTPError as e:
        logger.error(f"go2rtc recusou o stream {nome}: HTTP {e.code}")
    except Exception as e:
        logger.error(f"Erro registrando {nome} no go2rtc: {e}")
    return False


def _vigiar():
    """A cada 20 s confere o go2rtc e re-registra os streams se preciso."""
    while True:
        time.sleep(20)
        try:
            if not _streams:
                continue
            if not go2rtc_no_ar():
                logger.warning("go2rtc fora do ar — reiniciando...")
                if not garantir_go2rtc():
                    continue
            atuais = json.loads(_api('GET', '/api/streams') or b'{}')
            for nome, src in list(_streams.items()):
                if nome not in atuais:
                    _registrar_api(nome, src)
        except Exception as e:
            logger.debug(f"vigia go2rtc: {e}")


def registrar_camera_dvrip(cam_id: int, ip: str, porta, usuario: str,
                           senha: str) -> str:
    """
    Registra a câmera no go2rtc e devolve a URL RTSP local para o RRFS.
    Mesmo se o go2rtc estiver indisponível agora, devolve a URL (o vigia
    registra depois) — o stream do RRFS fica "offline" e tenta de novo.
    """
    global _vigia_ok
    nome = nome_stream(cam_id)
    src  = _src_dvrip(ip, porta, usuario, senha)
    _streams[nome] = src

    if garantir_go2rtc():
        if _registrar_api(nome, src):
            logger.info(f"[Cam {cam_id}] DVRIP {ip}:{porta or PORTA_DVRIP} → "
                        f"{url_rtsp_local(cam_id)}")
    else:
        logger.error(f"[Cam {cam_id}] go2rtc indisponível; "
                     "câmera DVRIP ficará offline até ele subir.")

    if not _vigia_ok:
        _vigia_ok = True
        threading.Thread(target=_vigiar, daemon=True,
                         name='go2rtc-vigia').start()
    return url_rtsp_local(cam_id)


def remover_camera_dvrip(cam_id: int):
    nome = nome_stream(cam_id)
    _streams.pop(nome, None)
    try:
        _api('DELETE', f"/api/streams?src={quote(nome, safe='')}")
    except Exception:
        pass


def is_dvrip(protocolo) -> bool:
    return (protocolo or '').strip().lower() == PROTOCOLO_DVRIP
