#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Módulo de Streaming
Gerencia conexões RTSP com controle de memória e threads
"""

import cv2
import threading
import logging
import time
import gc
from typing import Dict, Optional
from flask import Blueprint, Response, current_app
from flask_login import login_required

from modulos.database import get_conexao

logger = logging.getLogger('RRFS.streaming')
streaming_bp = Blueprint('streaming', __name__)

# ── Configurações de stream ────────────────────────────────────────────────
FRAME_WIDTH   = 640
FRAME_HEIGHT  = 360
JPEG_QUALITY  = 70          # 0-100 - menor = menos memória
RECONECT_WAIT = 5           # segundos entre tentativas
MAX_FALHAS    = 10          # falhas antes de parar tentativas
FRAME_SKIP    = 2           # captura 1 a cada N frames


class StreamCamera:
    """
    Gerencia a captura de um stream RTSP individual.
    Thread segura e sem vazamento de memória.
    """

    def __init__(self, cam_id: int, nome: str, url: str):
        self.cam_id  = cam_id
        self.nome    = nome
        self.url     = url
        self.online  = False
        self.ativo   = True         # False = encerra thread

        self._lock        = threading.Lock()
        self._frame_jpeg  : Optional[bytes] = None
        self._thread      : Optional[threading.Thread] = None
        self._falhas      = 0

        self._iniciar_thread()

    # ── Thread principal ───────────────────────────────────────────────
    def _iniciar_thread(self):
        self._thread = threading.Thread(
            target=self._loop_captura,
            name=f"stream-{self.cam_id}",
            daemon=True
        )
        self._thread.start()
        logger.info(f"[Cam {self.cam_id}] Thread iniciada: {self.nome}")

    def _loop_captura(self):
        """Loop principal de captura. Roda em thread dedicada."""
        while self.ativo:
            cap = None
            try:
                cap = self._conectar()
                if cap is None:
                    self._aguardar_reconexao()
                    continue

                self._falhas = 0
                self.online  = True
                logger.info(f"[Cam {self.cam_id}] Conectado: {self.nome}")

                count = 0
                while self.ativo:
                    ret, frame = cap.read()
                    if not ret:
                        logger.warning(
                            f"[Cam {self.cam_id}] Frame perdido."
                        )
                        break

                    count += 1
                    if count % FRAME_SKIP != 0:
                        continue

                    # Redimensiona para economizar memória
                    frame = cv2.resize(frame, (FRAME_WIDTH, FRAME_HEIGHT))

                    # Codifica para JPEG
                    ok, buf = cv2.imencode(
                        '.jpg', frame,
                        [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
                    )
                    if ok:
                        with self._lock:
                            self._frame_jpeg = buf.tobytes()
                        del buf

                    del frame
                    # Coleta lixo a cada 500 frames
                    if count % 500 == 0:
                        gc.collect()

            except Exception as e:
                logger.error(f"[Cam {self.cam_id}] Erro: {e}")
            finally:
                self.online = False
                if cap:
                    cap.release()
                    cap = None

            if self.ativo:
                self._aguardar_reconexao()

        logger.info(f"[Cam {self.cam_id}] Thread encerrada.")

    def _conectar(self) -> Optional[cv2.VideoCapture]:
        """Tenta conectar ao stream RTSP."""
        try:
            # FFMPEG com buffer mínimo para baixa latência
            os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = (
                'rtsp_transport;tcp|buffer_size;1024000'
            )
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)

            # Buffer mínimo
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            if not cap.isOpened():
                self._falhas += 1
                logger.warning(
                    f"[Cam {self.cam_id}] Falha {self._falhas}/{MAX_FALHAS}"
                )
                cap.release()
                return None

            return cap
        except Exception as e:
            logger.error(f"[Cam {self.cam_id}] Erro ao conectar: {e}")
            return None

    def _aguardar_reconexao(self):
        """Aguarda antes de tentar reconectar."""
        if self._falhas >= MAX_FALHAS:
            logger.error(
                f"[Cam {self.cam_id}] Máximo de falhas. "
                f"Aguardando {RECONECT_WAIT * 6}s..."
            )
            time.sleep(RECONECT_WAIT * 6)
        else:
            time.sleep(RECONECT_WAIT)

    # ── Acesso ao frame ────────────────────────────────────────────────
    def get_frame(self) -> Optional[bytes]:
        with self._lock:
            return self._frame_jpeg

    def encerrar(self):
        """Para a thread de captura com segurança."""
        logger.info(f"[Cam {self.cam_id}] Encerrando '{self.nome}'...")
        self.ativo  = False
        self.online = False
        with self._lock:
            self._frame_jpeg = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=8)
        logger.info(f"[Cam {self.cam_id}] Encerrado.")


# ── Gerenciador Global de Streams ──────────────────────────────────────────
import os

_streams: Dict[int, StreamCamera] = {}
_streams_lock = threading.Lock()


def adicionar_stream(cam_id: int, nome: str, url: str):
    """Adiciona e inicia um stream."""
    with _streams_lock:
        if cam_id in _streams:
            logger.info(f"[Cam {cam_id}] Stream já existe, reiniciando...")
            _streams[cam_id].encerrar()
        _streams[cam_id] = StreamCamera(cam_id, nome, url)


def remover_stream(cam_id: int):
    """Para e remove um stream."""
    with _streams_lock:
        if cam_id in _streams:
            _streams[cam_id].encerrar()
            del _streams[cam_id]
            gc.collect()


def status_stream(cam_id: int) -> bool:
    """Retorna True se câmera está online."""
    with _streams_lock:
        if cam_id in _streams:
            return _streams[cam_id].online
    return False


def listar_streams() -> list:
    """Lista status de todos os streams."""
    with _streams_lock:
        return [
            {
                'id': cid,
                'nome': s.nome,
                'online': s.online,
                'falhas': s._falhas
            }
            for cid, s in _streams.items()
        ]


def encerrar_todos_streams():
    """Encerra todos os streams - usado no shutdown."""
    with _streams_lock:
        for cam_id, stream in list(_streams.items()):
            stream.encerrar()
        _streams.clear()
    gc.collect()
    logger.info("Todos os streams encerrados.")


def iniciar_streams_salvos(db_path: str):
    """
    Chamado na inicialização do servidor.
    Conecta em todas as câmeras salvas no banco.
    """
    time.sleep(2)  # Aguarda Flask inicializar
    logger.info("Iniciando streams das câmeras salvas...")

    try:
        with get_conexao(db_path) as conn:
            rows = conn.execute(
                "SELECT id, nome, ip, porta, usuario, senha "
                "FROM cameras WHERE ativa = 1"
            ).fetchall()

        for row in rows:
            if row['usuario'] and row['senha']:
                url = (f"rtsp://{row['usuario']}:{row['senha']}"
                       f"@{row['ip']}:{row['porta']}/live/ch00_0")
            else:
                url = f"rtsp://{row['ip']}:{row['porta']}/live/ch00_0"

            adicionar_stream(row['id'], row['nome'], url)
            time.sleep(0.5)  # Evita flood de conexões

        logger.info(f"{len(rows)} câmera(s) iniciada(s).")
    except Exception as e:
        logger.error(f"Erro ao iniciar streams: {e}")


# ── Rota de streaming MJPEG ────────────────────────────────────────────────
FRAME_PLACEHOLDER = None  # Frame offline (carregado uma vez)


def get_frame_offline() -> bytes:
    """Retorna frame padrão quando câmera está offline."""
    global FRAME_PLACEHOLDER
    if FRAME_PLACEHOLDER is None:
        import numpy as np
        img = np.zeros((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
        # Texto "OFFLINE"
        cv2.putText(img, 'CAMERA OFFLINE', (120, 170),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2,
                    (0, 0, 180), 2, cv2.LINE_AA)
        cv2.putText(img, 'Aguardando conexao...', (130, 210),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (100, 100, 100), 1, cv2.LINE_AA)
        _, buf = cv2.imencode('.jpg', img,
                              [cv2.IMWRITE_JPEG_QUALITY, 60])
        FRAME_PLACEHOLDER = buf.tobytes()
        del img, buf

    return FRAME_PLACEHOLDER


def gerar_mjpeg(cam_id: int):
    """Gerador MJPEG para uma câmera."""
    while True:
        with _streams_lock:
            stream = _streams.get(cam_id)
            frame = stream.get_frame() if stream else None

        if frame is None:
            frame = get_frame_offline()

        yield (
            b'--frame\r\n'
            b'Content-Type: image/jpeg\r\n\r\n'
            + frame +
            b'\r\n'
        )
        time.sleep(0.04)  # ~25 fps máximo


@streaming_bp.route('/stream/<int:cam_id>')
@login_required
def video_feed(cam_id: int):
    """Endpoint de stream MJPEG."""
    return Response(
        gerar_mjpeg(cam_id),
        mimetype='multipart/x-mixed-replace; boundary=frame',
        headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0'
        }
    )
