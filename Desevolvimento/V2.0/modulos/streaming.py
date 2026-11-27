#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Módulo de Streaming
Captura RTSP · Detecção de Movimento Dia/Noite · Gravação automática
Otimizado para rodar 24h/7 sem vazamento de memória
"""

import cv2
import threading
import logging
import time
import gc
import os
import numpy as np
from typing  import Dict, Optional
from datetime import datetime

from flask              import Blueprint, Response
from flask_login        import login_required
from modulos.database   import get_conexao

logger       = logging.getLogger('RRFS.streaming')
streaming_bp = Blueprint('streaming', __name__)

# ── Configurações gerais ──────────────────────────────────────────────────
FRAME_WIDTH   = 640
FRAME_HEIGHT  = 360
JPEG_QUALITY  = 70
RECONECT_WAIT = 5
MAX_FALHAS    = 10
FRAME_SKIP    = 2          # processa 1 a cada 2 frames para o stream web

# ── Configurações de detecção ─────────────────────────────────────────────
MOTION_FPS           = 10   # FPS de gravação
MOTION_MIN_AREA      = 800  # área mínima de contorno (px²)
MOTION_BUFFER_BEFORE = 25   # frames pré-detecção gravados no buffer
MOTION_BUFFER_AFTER  = 80   # frames após última detecção antes de parar

# Limiar de luminosidade média para considerar imagem "noturna"
NIGHT_LUMA_THRESHOLD = 60   # 0-255 — abaixo disso = modo noturno


# ─────────────────────────────────────────────────────────────────────────
class StreamCamera:
    """
    Gerencia captura, detecção de movimento adaptativa (dia/noite)
    e gravação de eventos para uma câmera RTSP.
    Thread-safe · sem vazamento de memória.
    """

    def __init__(self, cam_id: int, nome: str, url: str,
                 gravar: bool = False, sensibilidade: int = 50,
                 gravacoes_path: str = '', thumbs_path: str = ''):

        self.cam_id         = cam_id
        self.nome           = nome
        self.url            = url
        self.online         = False
        self.ativo          = True
        self.gravar         = gravar
        self.sensibilidade  = sensibilidade   # 10-100
        self.gravacoes_path = gravacoes_path
        self.thumbs_path    = thumbs_path

        # Stream web
        self._lock       : threading.Lock      = threading.Lock()
        self._frame_jpeg : Optional[bytes]     = None
        self._thread     : Optional[threading.Thread] = None
        self._falhas     : int                 = 0

        # Detecção de movimento
        self._avg_frame           = None   # média acumulada (float32)
        self._frames_buffer       = []     # buffer circular pré-evento
        self._recording           = False
        self._video_writer        = None
        self._frames_sem_movimento= 0
        self._evento_path         = ''
        self._evento_frames       = 0
        self._modo_noturno        = False  # atualizado a cada frame

        self._iniciar_thread()

    # ── Thread ────────────────────────────────────────────────────────────
    def _iniciar_thread(self):
        self._thread = threading.Thread(
            target=self._loop_captura,
            name=f"stream-{self.cam_id}",
            daemon=True
        )
        self._thread.start()
        logger.info(f"[Cam {self.cam_id}] Thread iniciada: {self.nome}")

    def _loop_captura(self):
        while self.ativo:
            cap = None
            try:
                cap = self._conectar()
                if cap is None:
                    self._aguardar_reconexao()
                    continue

                self._falhas   = 0
                self.online    = True
                self._avg_frame = None          # reset ao reconectar
                logger.info(f"[Cam {self.cam_id}] Conectado: {self.nome}")

                count = 0
                while self.ativo:
                    ret, frame = cap.read()
                    if not ret:
                        logger.warning(f"[Cam {self.cam_id}] Frame perdido.")
                        break

                    count += 1

                    # ── Redimensiona ──────────────────────────────────
                    frame_res = cv2.resize(frame, (FRAME_WIDTH, FRAME_HEIGHT))

                    # ── Detecção / gravação (todos os frames) ─────────
                    if self.gravar:
                        movimento = self._detectar_movimento(frame_res)
                        self._gerenciar_gravacao(frame_res, movimento)

                    # ── JPEG para o stream web (1 a cada FRAME_SKIP) ──
                    if count % FRAME_SKIP == 0:
                        ok, buf = cv2.imencode(
                            '.jpg', frame_res,
                            [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
                        )
                        if ok:
                            with self._lock:
                                self._frame_jpeg = buf.tobytes()
                            del buf

                    del frame, frame_res

                    if count % 500 == 0:
                        gc.collect()

            except Exception as e:
                logger.error(f"[Cam {self.cam_id}] Erro: {e}")
            finally:
                self.online = False
                self._finalizar_gravacao()
                if cap:
                    cap.release()
                    cap = None

            if self.ativo:
                self._aguardar_reconexao()

        logger.info(f"[Cam {self.cam_id}] Thread encerrada.")

    # ── Conexão ───────────────────────────────────────────────────────────
    def _conectar(self) -> Optional[cv2.VideoCapture]:
        try:
            os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = (
                'rtsp_transport;tcp'
                '|buffer_size;4096000'
                '|max_delay;500000'
                '|reorder_queue_size;0'
                '|flags;low_delay'
            )
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 3)

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
        espera = RECONECT_WAIT * 6 if self._falhas >= MAX_FALHAS else RECONECT_WAIT
        time.sleep(espera)

    # ── Detecção de Movimento (Dia + Noite P&B) ───────────────────────────
    def _detectar_movimento(self, frame: np.ndarray) -> bool:
        """
        Detecção adaptativa:
        • Analisa luminosidade média → define modo dia/noite
        • Noite (P&B): blur maior, threshold mais alto, ignora ruído CMOS
        • Ignora mudança global de luminosidade (ligar/apagar luz)
        Retorna True se movimento real detectado.
        """
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            # ── Detecta modo noturno ──────────────────────────────────
            luma_media = float(np.mean(gray))
            self._modo_noturno = (luma_media < NIGHT_LUMA_THRESHOLD)

            # ── Blur adaptativo ───────────────────────────────────────
            # Noite: blur maior para suprimir ruído granulado do sensor
            ksize = (31, 31) if self._modo_noturno else (21, 21)
            gray_blur = cv2.GaussianBlur(gray, ksize, 0)

            # ── Inicializa média na primeira vez ──────────────────────
            if self._avg_frame is None:
                self._avg_frame = gray_blur.astype(np.float32)
                return False

            # ── Verifica mudança global de luminosidade ───────────────
            # (ex.: luz acende/apaga) → NÃO é movimento de objeto
            avg_luma_anterior = float(np.mean(self._avg_frame))
            delta_global = abs(luma_media - avg_luma_anterior)
            if delta_global > 25:
                # Reseta média para se adaptar rapidamente
                self._avg_frame = gray_blur.astype(np.float32)
                return False

            # ── Atualiza média móvel ──────────────────────────────────
            # Noite: alpha menor → média mais lenta → menos falsos positivos
            alpha = 0.3 if self._modo_noturno else 0.5
            cv2.accumulateWeighted(gray_blur, self._avg_frame, alpha)
            frame_delta = cv2.absdiff(
                gray_blur,
                cv2.convertScaleAbs(self._avg_frame)
            )

            # ── Threshold adaptativo por sensibilidade e modo ─────────
            # sensibilidade 10-100 → thresh 200-30 (dia)
            # sensibilidade 10-100 → thresh 230-60 (noite, mais restrito)
            base_min = 60  if self._modo_noturno else 30
            base_max = 230 if self._modo_noturno else 200
            thresh_val = int(
                base_max - ((self.sensibilidade - 10) / 90) * (base_max - base_min)
            )

            _, thresh = cv2.threshold(
                frame_delta, thresh_val, 255, cv2.THRESH_BINARY
            )

            # Dilation menor à noite para não unir grãos de ruído
            iter_dilate = 1 if self._modo_noturno else 2
            thresh = cv2.dilate(thresh, None, iterations=iter_dilate)

            # ── Contornos ─────────────────────────────────────────────
            contours, _ = cv2.findContours(
                thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )

            # Área mínima maior à noite (elimina grãos residuais)
            min_area = MOTION_MIN_AREA * 1.5 if self._modo_noturno else MOTION_MIN_AREA
            movimento = any(
                cv2.contourArea(c) > min_area for c in contours
            )

            del gray, gray_blur, frame_delta, thresh
            return movimento

        except Exception as e:
            logger.error(f"[Cam {self.cam_id}] Erro na detecção: {e}")
            return False

    # ── Gerenciamento de Gravação ─────────────────────────────────────────
    def _gerenciar_gravacao(self, frame: np.ndarray, movimento: bool):
        # Mantém buffer circular de frames pré-evento
        frame_copy = frame.copy()
        self._frames_buffer.append(frame_copy)
        if len(self._frames_buffer) > MOTION_BUFFER_BEFORE:
            old = self._frames_buffer.pop(0)
            del old

        if movimento:
            self._frames_sem_movimento = 0
            if not self._recording:
                self._iniciar_gravacao()
        else:
            if self._recording:
                self._frames_sem_movimento += 1
                if self._frames_sem_movimento > MOTION_BUFFER_AFTER:
                    self._finalizar_gravacao()

        # Grava frame atual
        if self._recording and self._video_writer is not None:
            self._video_writer.write(frame)
            self._evento_frames += 1

    def _iniciar_gravacao(self):
        try:
            ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
            modo_tag = "noite" if self._modo_noturno else "dia"
            filename = f"cam{self.cam_id}_{ts}_{modo_tag}.mp4"
            filepath = os.path.join(self.gravacoes_path, filename)

            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            self._video_writer = cv2.VideoWriter(
                filepath, fourcc, MOTION_FPS,
                (FRAME_WIDTH, FRAME_HEIGHT)
            )

            # Escreve buffer pré-evento
            for f in self._frames_buffer:
                self._video_writer.write(f)

            self._recording            = True
            self._evento_path          = filepath
            self._evento_frames        = len(self._frames_buffer)
            self._frames_sem_movimento = 0

            modo_str = "🌙 noturno" if self._modo_noturno else "☀ diurno"
            logger.info(
                f"[Cam {self.cam_id}] ► Gravação iniciada [{modo_str}]: {filename}"
            )
        except Exception as e:
            logger.error(f"[Cam {self.cam_id}] Erro ao iniciar gravação: {e}")
            self._recording = False

    def _finalizar_gravacao(self):
        if not self._recording:
            return
        try:
            if self._video_writer:
                self._video_writer.release()
                self._video_writer = None

            filepath = self._evento_path
            if not filepath or not os.path.exists(filepath):
                return

            # Descarta vídeos muito pequenos (< 50 KB)
            tamanho = os.path.getsize(filepath)
            if tamanho < 50 * 1024:
                os.remove(filepath)
                logger.warning(f"[Cam {self.cam_id}] Vídeo descartado (muito pequeno).")
                return

            # Gera thumbnail
            thumb_name = os.path.basename(filepath).replace('.mp4', '.jpg')
            thumb_path = os.path.join(self.thumbs_path, thumb_name)
            self._gerar_thumbnail(filepath, thumb_path)

            # Salva no banco
            duracao  = max(1, int(self._evento_frames / MOTION_FPS))
            db_path  = os.path.join(
                os.path.dirname(self.gravacoes_path), 'config', 'sistema.db'
            )
            with get_conexao(db_path) as conn:
                conn.execute(
                    """INSERT INTO eventos
                       (camera_id, arquivo_video, arquivo_thumb,
                        duracao, tamanho_bytes)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        self.cam_id,
                        os.path.basename(filepath),
                        thumb_name,
                        duracao,
                        tamanho
                    )
                )
            logger.info(
                f"[Cam {self.cam_id}] ■ Gravação salva: "
                f"{duracao}s · {tamanho/1024/1024:.1f} MB"
            )
        except Exception as e:
            logger.error(f"[Cam {self.cam_id}] Erro ao finalizar gravação: {e}")
        finally:
            self._recording            = False
            self._evento_path          = ''
            self._evento_frames        = 0
            self._frames_sem_movimento = 0

    def _gerar_thumbnail(self, video_path: str, thumb_path: str):
        try:
            cap = cv2.VideoCapture(video_path)
            ret, frame = cap.read()
            if ret:
                cv2.imwrite(
                    thumb_path, frame,
                    [cv2.IMWRITE_JPEG_QUALITY, 85]
                )
            cap.release()
        except Exception as e:
            logger.error(f"[Cam {self.cam_id}] Erro ao gerar thumbnail: {e}")

    # ── Interface pública ─────────────────────────────────────────────────
    def get_frame(self) -> Optional[bytes]:
        with self._lock:
            return self._frame_jpeg

    def atualizar_config(self, gravar: bool, sensibilidade: int):
        self.gravar       = gravar
        self.sensibilidade = sensibilidade
        if not gravar and self._recording:
            self._finalizar_gravacao()
        logger.info(
            f"[Cam {self.cam_id}] Config atualizada: "
            f"gravar={gravar} sensibilidade={sensibilidade}%"
        )

    def encerrar(self):
        logger.info(f"[Cam {self.cam_id}] Encerrando '{self.nome}'...")
        self.ativo  = False
        self.online = False
        self._finalizar_gravacao()
        with self._lock:
            self._frame_jpeg = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=8)
        # Libera buffer
        self._frames_buffer.clear()
        gc.collect()
        logger.info(f"[Cam {self.cam_id}] Encerrado.")


# ── Gerenciador Global de Streams ─────────────────────────────────────────
_streams      : Dict[int, StreamCamera] = {}
_streams_lock : threading.Lock          = threading.Lock()


def adicionar_stream(cam_id: int, nome: str, url: str,
                     gravar: bool = False, sensibilidade: int = 50,
                     gravacoes_path: str = '', thumbs_path: str = ''):
    with _streams_lock:
        if cam_id in _streams:
            _streams[cam_id].encerrar()
        _streams[cam_id] = StreamCamera(
            cam_id, nome, url, gravar, sensibilidade,
            gravacoes_path, thumbs_path
        )


def remover_stream(cam_id: int):
    with _streams_lock:
        if cam_id in _streams:
            _streams[cam_id].encerrar()
            del _streams[cam_id]
            gc.collect()


def atualizar_config_stream(cam_id: int, gravar: bool, sensibilidade: int):
    with _streams_lock:
        if cam_id in _streams:
            _streams[cam_id].atualizar_config(gravar, sensibilidade)


def status_stream(cam_id: int) -> bool:
    with _streams_lock:
        return _streams[cam_id].online if cam_id in _streams else False


def listar_streams() -> list:
    with _streams_lock:
        return [
            {
                'id'      : cid,
                'nome'    : s.nome,
                'online'  : s.online,
                'gravando': s._recording,
                'noturno' : s._modo_noturno,
                'falhas'  : s._falhas,
            }
            for cid, s in _streams.items()
        ]


def encerrar_todos_streams():
    with _streams_lock:
        for s in _streams.values():
            s.encerrar()
        _streams.clear()
    gc.collect()
    logger.info("Todos os streams encerrados.")


def iniciar_streams_salvos(db_path: str,
                            gravacoes_path: str,
                            thumbs_path: str):
    time.sleep(2)
    logger.info("Iniciando streams das câmeras salvas...")
    try:
        with get_conexao(db_path) as conn:
            rows = conn.execute(
                "SELECT id, nome, ip, porta, usuario, senha, "
                "gravar, sensibilidade "
                "FROM cameras WHERE ativa = 1"
            ).fetchall()

        for row in rows:
            if row['usuario'] and row['senha']:
                url = (
                    f"rtsp://{row['usuario']}:{row['senha']}"
                    f"@{row['ip']}:{row['porta']}/live/ch00_0"
                )
            else:
                url = f"rtsp://{row['ip']}:{row['porta']}/live/ch00_0"

            adicionar_stream(
                row['id'], row['nome'], url,
                bool(row['gravar']), row['sensibilidade'],
                gravacoes_path, thumbs_path
            )
            time.sleep(0.5)

        logger.info(f"{len(rows)} câmera(s) iniciada(s).")
    except Exception as e:
        logger.error(f"Erro ao iniciar streams: {e}")


# ── Frame offline ─────────────────────────────────────────────────────────
_frame_offline: Optional[bytes] = None


def get_frame_offline() -> bytes:
    global _frame_offline
    if _frame_offline is None:
        img = np.zeros((FRAME_HEIGHT, FRAME_WIDTH, 3), dtype=np.uint8)
        cv2.putText(img, 'CAMERA OFFLINE', (100, 170),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2,
                    (0, 0, 180), 2, cv2.LINE_AA)
        cv2.putText(img, 'Aguardando conexao...', (120, 210),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (100, 100, 100), 1, cv2.LINE_AA)
        _, buf = cv2.imencode(
            '.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 60]
        )
        _frame_offline = buf.tobytes()
        del img, buf
    return _frame_offline


def gerar_mjpeg(cam_id: int):
    while True:
        with _streams_lock:
            stream = _streams.get(cam_id)
            frame  = stream.get_frame() if stream else None

        yield (
            b'--frame\r\n'
            b'Content-Type: image/jpeg\r\n\r\n'
            + (frame if frame else get_frame_offline())
            + b'\r\n'
        )
        time.sleep(0.04)   # ~25 fps máximo


# ── Rota MJPEG ────────────────────────────────────────────────────────────
@streaming_bp.route('/stream/<int:cam_id>')
@login_required
def video_feed(cam_id: int):
    return Response(
        gerar_mjpeg(cam_id),
        mimetype='multipart/x-mixed-replace; boundary=frame',
        headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma'       : 'no-cache',
            'Expires'      : '0',
        }
    )
