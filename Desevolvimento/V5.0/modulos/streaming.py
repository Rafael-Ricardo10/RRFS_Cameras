#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Módulo de Streaming
Captura RTSP · Detecção de Movimento com Zona (ROI) · Dia/Noite · Gravação
Otimizado para 24h/7 sem vazamento de memória
"""

import cv2
import threading
import logging
import time
import gc
import os
import json
import shutil
import subprocess
import numpy as np
from typing   import Dict, List, Optional
from datetime import datetime

from flask            import Blueprint, Response
from flask_login      import login_required
from modulos.database import get_conexao

logger       = logging.getLogger('RRFS.streaming')
streaming_bp = Blueprint('streaming', __name__)

# ── Configurações gerais ──────────────────────────────────────────────────
FRAME_WIDTH   = 640
FRAME_HEIGHT  = 360
JPEG_QUALITY  = 70
RECONECT_WAIT = 5
MAX_FALHAS    = 10
FRAME_SKIP    = 2

# ── Configurações de detecção ─────────────────────────────────────────────
MOTION_FPS           = 10
MOTION_BUFFER_BEFORE = 25
MOTION_BUFFER_AFTER  = 80
NIGHT_LUMA_THRESHOLD = 60

# ── Watermark ─────────────────────────────────────────────────────────────
WATERMARK_TITULO = "RRFS Cameras"

# ── Detecta FFmpeg ────────────────────────────────────────────────────────
_FFMPEG_PATH = shutil.which('ffmpeg')
_FFMPEG_OK   = _FFMPEG_PATH is not None

if _FFMPEG_OK:
    logger.info(f"FFmpeg encontrado: {_FFMPEG_PATH}")
else:
    logger.warning(
        "FFmpeg NÃO encontrado! Instale com: sudo apt install ffmpeg"
    )


class StreamCamera:
    """
    Gerencia captura RTSP, detecção de movimento com zona (ROI),
    modo dia/noite adaptativo e gravação de eventos com watermark.
    Suporta TCP, UDP ou automático (tenta TCP → UDP).
    Thread-safe · sem vazamento de memória.
    """

    def __init__(self, cam_id: int, nome: str, url: str,
                 gravar: bool = False, sensibilidade: int = 50,
                 gravacoes_path: str = '', thumbs_path: str = '',
                 zona_pontos: str = '', transporte: str = 'auto'):

        self.cam_id         = cam_id
        self.nome           = nome
        self.url            = url
        self.online         = False
        self.ativo          = True
        self.gravar         = gravar
        self.sensibilidade  = sensibilidade
        self.gravacoes_path = gravacoes_path
        self.thumbs_path    = thumbs_path
        self.transporte     = transporte  # 'tcp', 'udp' ou 'auto'

        # Zona de detecção (ROI)
        self._zona_pontos : List                 = self._parse_zona(zona_pontos)
        self._zona_mascara: Optional[np.ndarray] = None
        self._build_mascara()

        # Stream web
        self._lock       = threading.Lock()
        self._frame_jpeg : Optional[bytes]            = None
        self._thread     : Optional[threading.Thread] = None
        self._falhas     = 0

        # MOG2
        self._mog2         : Optional[cv2.BackgroundSubtractorMOG2] = None
        self._mog2_warmup  = 0
        self._modo_noturno = False

        # Gravação
        self._frames_buffer        : list = []
        self._recording            = False
        self._video_writer         = None
        self._frames_sem_movimento = 0
        self._evento_path          = ''
        self._evento_frames        = 0

        # Debug
        self._frame_count    = 0
        self._last_log_frame = 0

        self._iniciar_thread()
        logger.info(
            f"[Cam {self.cam_id}] Inicializada · gravar={self.gravar} "
            f"sensib={self.sensibilidade}% "
            f"transporte={self.transporte} "
            f"zona={'SIM' if self._zona_pontos else 'NÃO'} "
            f"ffmpeg={'OK' if _FFMPEG_OK else 'AUSENTE'}"
        )

    # ── Zona / Máscara ────────────────────────────────────────────────────
    def _parse_zona(self, zona_str: str) -> List:
        if not zona_str:
            return []
        try:
            pts = json.loads(zona_str)
            if isinstance(pts, list) and len(pts) >= 3:
                return pts
        except Exception:
            pass
        return []

    def _build_mascara(self):
        self._zona_mascara = None
        if len(self._zona_pontos) >= 3:
            mascara = np.zeros((FRAME_HEIGHT, FRAME_WIDTH), dtype=np.uint8)
            pts = np.array(self._zona_pontos, dtype=np.int32)
            cv2.fillPoly(mascara, [pts], 255)
            self._zona_mascara = mascara
            logger.info(
                f"[Cam {self.cam_id}] Zona definida "
                f"com {len(self._zona_pontos)} pontos."
            )

    def atualizar_zona(self, zona_pontos: str):
        self._zona_pontos = self._parse_zona(zona_pontos)
        self._build_mascara()
        self._resetar_mog2()
        logger.info(f"[Cam {self.cam_id}] Zona atualizada em tempo real.")

    # ── MOG2 ─────────────────────────────────────────────────────────────
    def _criar_mog2(self):
        var_max    = 200.0
        var_min    = 16.0
        ratio      = (self.sensibilidade - 10) / 90.0
        var_thresh = int(var_max - ratio * (var_max - var_min))
        var_thresh = max(int(var_min), min(int(var_max), var_thresh))

        self._mog2 = cv2.createBackgroundSubtractorMOG2(
            history=200,
            varThreshold=var_thresh,
            detectShadows=False
        )
        self._mog2_warmup = 0
        logger.info(
            f"[Cam {self.cam_id}] MOG2 criado · "
            f"varThreshold={var_thresh} (sensib={self.sensibilidade}%)"
        )

    def _resetar_mog2(self):
        self._criar_mog2()

    # ── Watermark ─────────────────────────────────────────────────────────
    def _desenhar_watermark(self, frame: np.ndarray) -> np.ndarray:
        """
        Desenha no canto superior esquerdo:
          Linha 1: RRFS Cameras          (tamanho normal)
          Linha 2: DD/MM/AAAA HH:MM:SS   (tamanho menor, abaixo)

        Cada linha tem sombra preta para legibilidade em qualquer fundo.
        Chamada UMA única vez por frame - sem duplicação.
        """
        agora  = datetime.now()
        linha1 = WATERMARK_TITULO
        linha2 = agora.strftime("%d/%m/%Y  %H:%M:%S")

        fonte      = cv2.FONT_HERSHEY_SIMPLEX
        cor_txt    = (255, 255, 255)  # branco
        cor_sombra = (0,   0,   0)   # preto

        # ── Linha 1: título ───────────────────────────────────────────────
        escala1 = 0.50
        espess1 = 1
        x1, y1 = 8, 22

        # Sombra
        cv2.putText(frame, linha1, (x1 + 1, y1 + 1),
                    fonte, escala1, cor_sombra, espess1 + 1, cv2.LINE_AA)
        # Texto branco
        cv2.putText(frame, linha1, (x1, y1),
                    fonte, escala1, cor_txt, espess1, cv2.LINE_AA)

        # ── Linha 2: data/hora ────────────────────────────────────────────
        escala2 = 0.42
        espess2 = 1
        x2, y2 = 8, 40   # 18px abaixo da linha 1

        # Sombra
        cv2.putText(frame, linha2, (x2 + 1, y2 + 1),
                    fonte, escala2, cor_sombra, espess2 + 1, cv2.LINE_AA)
        # Texto branco
        cv2.putText(frame, linha2, (x2, y2),
                    fonte, escala2, cor_txt, espess2, cv2.LINE_AA)

        return frame

    # ── Thread principal ──────────────────────────────────────────────────
    def _iniciar_thread(self):
        self._thread = threading.Thread(
            target=self._loop_captura,
            name=f"stream-{self.cam_id}",
            daemon=True
        )
        self._thread.start()

    def _loop_captura(self):
        while self.ativo:
            cap = None
            try:
                self._criar_mog2()
                cap = self._conectar()
                if cap is None:
                    self._aguardar_reconexao()
                    continue

                self._falhas      = 0
                self.online       = True
                self._frame_count = 0
                self._last_log_frame = 0
                logger.info(
                    f"[Cam {self.cam_id}] ✓ Conectado: {self.nome}"
                )

                count = 0
                while self.ativo:
                    ret, frame = cap.read()
                    if not ret:
                        logger.warning(
                            f"[Cam {self.cam_id}] Frame perdido."
                        )
                        break

                    count += 1
                    self._frame_count += 1

                    # ── Frame base: redimensionado, limpo, sem overlay ─────
                    frame_res = cv2.resize(
                        frame, (FRAME_WIDTH, FRAME_HEIGHT)
                    )

                    # ── Gravação ──────────────────────────────────────────
                    # Detecção usa frame limpo
                    # Frame gravado = limpo + watermark (UMA vez)
                    if self.gravar:
                        movimento  = self._detectar_movimento(frame_res)
                        frame_grav = frame_res.copy()
                        self._desenhar_watermark(frame_grav)
                        self._gerenciar_gravacao(frame_grav, movimento)
                        del frame_grav

                    # ── Stream web ────────────────────────────────────────
                    # Frame web = limpo + zona + watermark (UMA vez)
                    if count % FRAME_SKIP == 0:
                        frame_vis = frame_res.copy()
                        self._desenhar_zona(frame_vis)
                        self._desenhar_watermark(frame_vis)

                        ok, buf = cv2.imencode(
                            '.jpg', frame_vis,
                            [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
                        )
                        if ok:
                            with self._lock:
                                self._frame_jpeg = buf.tobytes()
                            del buf
                        del frame_vis

                    del frame, frame_res

                    if count % 500 == 0:
                        gc.collect()

            except Exception as e:
                logger.error(
                    f"[Cam {self.cam_id}] Erro no loop: {e}",
                    exc_info=True
                )
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
    def _tentar_conectar_com(self, protocolo: str) -> Optional[cv2.VideoCapture]:
        """
        Tenta conectar com o protocolo especificado (tcp ou udp).
        Retorna cap somente se conseguiu abrir E ler pelo menos 1 frame.
        """
        cap = None
        try:
            if protocolo == 'tcp':
                opcoes = (
                    'rtsp_transport;tcp'
                    '|buffer_size;4096000'
                    '|max_delay;500000'
                    '|reorder_queue_size;0'
                    '|flags;low_delay'
                )
            else:  # udp
                opcoes = (
                    'rtsp_transport;udp'
                    '|buffer_size;4096000'
                    '|max_delay;1000000'
                    '|reorder_queue_size;500'
                )

            os.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS'] = opcoes
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 3)

            if not cap.isOpened():
                cap.release()
                logger.debug(
                    f"[Cam {self.cam_id}] {protocolo.upper()}: "
                    f"não abriu."
                )
                return None

            # Tenta ler até 5 frames (câmeras lentas demoram)
            leu_frame = False
            for _ in range(5):
                ret, frame = cap.read()
                if ret and frame is not None and frame.size > 0:
                    leu_frame = True
                    del frame
                    break
                time.sleep(0.5)

            if not leu_frame:
                cap.release()
                logger.debug(
                    f"[Cam {self.cam_id}] {protocolo.upper()}: "
                    f"abriu mas não leu frame válido."
                )
                return None

            logger.info(
                f"[Cam {self.cam_id}] ✓ Conectado via "
                f"{protocolo.upper()}."
            )
            return cap

        except Exception as e:
            logger.warning(
                f"[Cam {self.cam_id}] Erro {protocolo.upper()}: {e}"
            )
            try:
                if cap:
                    cap.release()
            except Exception:
                pass
            return None

    def _conectar(self) -> Optional[cv2.VideoCapture]:
        """
        Conecta de acordo com o transporte configurado:
          'tcp'  → só tenta TCP
          'udp'  → só tenta UDP
          'auto' → tenta TCP primeiro; se falhar, tenta UDP
        """
        modo = (self.transporte or 'auto').lower().strip()

        if modo == 'tcp':
            cap = self._tentar_conectar_com('tcp')
            if cap is None:
                self._falhas += 1
                logger.warning(
                    f"[Cam {self.cam_id}] Falha TCP "
                    f"{self._falhas}/{MAX_FALHAS}"
                )
            return cap

        elif modo == 'udp':
            cap = self._tentar_conectar_com('udp')
            if cap is None:
                self._falhas += 1
                logger.warning(
                    f"[Cam {self.cam_id}] Falha UDP "
                    f"{self._falhas}/{MAX_FALHAS}"
                )
            return cap

        else:  # auto
            # ── Tentativa TCP ─────────────────────────────────────────────
            cap = self._tentar_conectar_com('tcp')
            if cap is not None:
                return cap

            # ── TCP falhou → tenta UDP ────────────────────────────────────
            logger.info(
                f"[Cam {self.cam_id}] TCP falhou, tentando UDP..."
            )
            cap = self._tentar_conectar_com('udp')
            if cap is not None:
                return cap

            # ── Ambos falharam ────────────────────────────────────────────
            self._falhas += 1
            logger.warning(
                f"[Cam {self.cam_id}] Falha TCP+UDP "
                f"{self._falhas}/{MAX_FALHAS}"
            )
            return None

    def _aguardar_reconexao(self):
        espera = RECONECT_WAIT * 6 if self._falhas >= MAX_FALHAS \
                 else RECONECT_WAIT
        logger.info(
            f"[Cam {self.cam_id}] Reconectando em {espera}s..."
        )
        time.sleep(espera)

    # ── Zona visual ───────────────────────────────────────────────────────
    def _desenhar_zona(self, frame: np.ndarray) -> np.ndarray:
        """Desenha o polígono de zona diretamente no frame (in-place)."""
        if len(self._zona_pontos) < 2:
            return frame
        try:
            pts     = np.array(self._zona_pontos, dtype=np.int32)
            overlay = frame.copy()
            if len(self._zona_pontos) >= 3:
                cv2.fillPoly(overlay, [pts], (0, 170, 255))
                cv2.addWeighted(overlay, 0.15, frame, 0.85, 0, frame)
            cv2.polylines(
                frame, [pts], isClosed=True,
                color=(0, 170, 255), thickness=2,
                lineType=cv2.LINE_AA
            )
            for pt in self._zona_pontos:
                cv2.circle(frame, tuple(pt), 4, (0, 200, 255), -1)
            del overlay
        except Exception:
            pass
        return frame

    # ── Detecção de Movimento (MOG2) ──────────────────────────────────────
    def _detectar_movimento(self, frame: np.ndarray) -> bool:
        """
        Detecção com MOG2. Usa frame limpo (sem watermark/zona).
        """
        try:
            if self._mog2 is None:
                self._criar_mog2()

            gray       = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            luma_media = float(np.mean(gray))
            self._modo_noturno = (luma_media < NIGHT_LUMA_THRESHOLD)

            gray_blur = cv2.GaussianBlur(gray, (5, 5), 0)
            fg_mask   = self._mog2.apply(gray_blur)

            # Aquecimento: ignora primeiros 30 frames
            self._mog2_warmup += 1
            if self._mog2_warmup <= 30:
                del gray, gray_blur, fg_mask
                return False

            # Aplica máscara de zona (ROI)
            if self._zona_mascara is not None:
                fg_mask = cv2.bitwise_and(
                    fg_mask, fg_mask, mask=self._zona_mascara
                )

            # Remove ruído
            kernel  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
            fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN,  kernel)
            fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_CLOSE, kernel)
            fg_mask = cv2.dilate(fg_mask, kernel, iterations=2)

            # Área mínima baseada na sensibilidade
            area_max = 3000.0
            area_min = 150.0
            ratio    = (self.sensibilidade - 10) / 90.0
            min_area = int(area_max - ratio * (area_max - area_min))
            min_area = max(int(area_min), min(int(area_max), min_area))

            contours, _ = cv2.findContours(
                fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )

            movimento_detectado = False
            maior_area          = 0

            for c in contours:
                area = cv2.contourArea(c)
                if area > min_area:
                    movimento_detectado = True
                    if area > maior_area:
                        maior_area = area

            # Log periódico
            delta_log = self._frame_count - self._last_log_frame
            if movimento_detectado or delta_log >= 150:
                modo = "🌙noturno" if self._modo_noturno else "☀️diurno"
                logger.info(
                    f"[Cam {self.cam_id}] [{modo}] "
                    f"mov={'SIM ►' if movimento_detectado else 'não'} "
                    f"sensib={self.sensibilidade}% "
                    f"min_area={min_area} "
                    f"maior_area={maior_area:.0f} "
                    f"luma={luma_media:.1f} "
                    f"warmup={self._mog2_warmup}"
                )
                self._last_log_frame = self._frame_count

            del gray, gray_blur, fg_mask
            return movimento_detectado

        except Exception as e:
            logger.error(
                f"[Cam {self.cam_id}] Erro na detecção: {e}",
                exc_info=True
            )
            return False

    # ── Gravação ──────────────────────────────────────────────────────────
    def _gerenciar_gravacao(self, frame: np.ndarray, movimento: bool):
        """
        Recebe frame JÁ com watermark aplicado.
        Gerencia buffer pré-evento e escrita no VideoWriter.
        """
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

            if not self._video_writer.isOpened():
                logger.error(
                    f"[Cam {self.cam_id}] Falha ao criar VideoWriter!"
                )
                self._video_writer = None
                return

            # Escreve buffer pré-evento (frames com watermark)
            for f in self._frames_buffer:
                self._video_writer.write(f)

            self._recording            = True
            self._evento_path          = filepath
            self._evento_frames        = len(self._frames_buffer)
            self._frames_sem_movimento = 0

            modo_str = "🌙 noturno" if self._modo_noturno else "☀️ diurno"
            zona_str = "com zona"   if self._zona_mascara is not None \
                       else "sem zona"
            logger.info(
                f"[Cam {self.cam_id}] ► GRAVAÇÃO INICIADA "
                f"[{modo_str}][{zona_str}]: {filename}"
            )
        except Exception as e:
            logger.error(
                f"[Cam {self.cam_id}] Erro ao iniciar gravação: {e}",
                exc_info=True
            )
            self._recording = False

    def _converter_h264_ffmpeg(self, filepath: str) -> bool:
        """Converte mp4v → H.264 via FFmpeg."""
        if not _FFMPEG_OK:
            return False

        filepath_tmp = filepath + '.tmp.mp4'
        try:
            cmd = [
                _FFMPEG_PATH,
                '-y',
                '-i', filepath,
                '-c:v', 'libx264',
                '-preset', 'ultrafast',
                '-crf', '23',
                '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart',
                '-an',
                filepath_tmp
            ]
            logger.info(f"[Cam {self.cam_id}] Convertendo → H.264...")
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=60
            )
            if result.returncode != 0:
                erro = result.stderr.decode(errors='ignore')[-300:]
                logger.error(
                    f"[Cam {self.cam_id}] FFmpeg erro: {erro}"
                )
                if os.path.exists(filepath_tmp):
                    os.remove(filepath_tmp)
                return False

            if not os.path.exists(filepath_tmp) or \
               os.path.getsize(filepath_tmp) < 10 * 1024:
                if os.path.exists(filepath_tmp):
                    os.remove(filepath_tmp)
                return False

            os.replace(filepath_tmp, filepath)
            logger.info(
                f"[Cam {self.cam_id}] ✓ H.264: "
                f"{os.path.getsize(filepath)/1024/1024:.1f} MB"
            )
            return True

        except subprocess.TimeoutExpired:
            logger.error(f"[Cam {self.cam_id}] Timeout FFmpeg.")
            if os.path.exists(filepath_tmp):
                os.remove(filepath_tmp)
            return False
        except Exception as e:
            logger.error(f"[Cam {self.cam_id}] Erro FFmpeg: {e}")
            if os.path.exists(filepath_tmp):
                os.remove(filepath_tmp)
            return False

    def _finalizar_gravacao(self):
        if not self._recording:
            return
        try:
            if self._video_writer:
                self._video_writer.release()
                self._video_writer = None

            filepath = self._evento_path
            if not filepath or not os.path.exists(filepath):
                logger.warning(
                    f"[Cam {self.cam_id}] Arquivo não encontrado: {filepath}"
                )
                return

            tamanho = os.path.getsize(filepath)
            if tamanho < 50 * 1024:
                os.remove(filepath)
                logger.warning(
                    f"[Cam {self.cam_id}] Vídeo descartado "
                    f"({tamanho} bytes - muito pequeno)."
                )
                return

            # Converte para H.264
            self._converter_h264_ffmpeg(filepath)

            if os.path.exists(filepath):
                tamanho = os.path.getsize(filepath)

            # Gera thumbnail
            thumb_name = os.path.basename(filepath).replace('.mp4', '.jpg')
            thumb_path = os.path.join(self.thumbs_path, thumb_name)
            self._gerar_thumbnail(filepath, thumb_path)

            # Salva no banco
            duracao = max(1, int(self._evento_frames / MOTION_FPS))
            db_path = os.path.join(
                os.path.dirname(self.gravacoes_path), 'config', 'sistema.db'
            )
            with get_conexao(db_path) as conn:
                conn.execute(
                    """INSERT INTO eventos
                       (camera_id, arquivo_video, arquivo_thumb,
                        duracao, tamanho_bytes)
                       VALUES (?, ?, ?, ?, ?)""",
                    (self.cam_id,
                     os.path.basename(filepath),
                     thumb_name,
                     duracao,
                     tamanho)
                )
            logger.info(
                f"[Cam {self.cam_id}] ■ SALVO: "
                f"{duracao}s · {tamanho/1024/1024:.1f} MB"
            )
        except Exception as e:
            logger.error(
                f"[Cam {self.cam_id}] Erro ao finalizar: {e}",
                exc_info=True
            )
        finally:
            self._recording            = False
            self._evento_path          = ''
            self._evento_frames        = 0
            self._frames_sem_movimento = 0

    def _gerar_thumbnail(self, video_path: str, thumb_path: str):
        """Gera thumbnail do primeiro frame do vídeo."""
        try:
            cap = cv2.VideoCapture(video_path)
            ret, frame = cap.read()
            if ret:
                cv2.imwrite(
                    thumb_path, frame,
                    [cv2.IMWRITE_JPEG_QUALITY, 85]
                )
            else:
                logger.warning(
                    f"[Cam {self.cam_id}] Falha ao gerar thumbnail."
                )
            cap.release()
        except Exception as e:
            logger.error(f"[Cam {self.cam_id}] Erro thumbnail: {e}")

    # ── Interface pública ─────────────────────────────────────────────────
    def get_frame(self) -> Optional[bytes]:
        with self._lock:
            return self._frame_jpeg

    def atualizar_config(self, gravar: bool, sensibilidade: int):
        mudou_sensib   = (sensibilidade != self.sensibilidade)
        self.gravar        = gravar
        self.sensibilidade = sensibilidade
        if mudou_sensib:
            self._resetar_mog2()
        logger.info(
            f"[Cam {self.cam_id}] Config: gravar={gravar} "
            f"sensib={sensibilidade}%"
        )
        if not gravar and self._recording:
            self._finalizar_gravacao()

    def encerrar(self):
        logger.info(
            f"[Cam {self.cam_id}] Encerrando '{self.nome}'..."
        )
        self.ativo  = False
        self.online = False
        self._finalizar_gravacao()
        with self._lock:
            self._frame_jpeg = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=8)
        self._frames_buffer.clear()
        if self._zona_mascara is not None:
            del self._zona_mascara
            self._zona_mascara = None
        self._mog2 = None
        gc.collect()
        logger.info(f"[Cam {self.cam_id}] ✓ Encerrado.")


# ── Gerenciador Global ────────────────────────────────────────────────────
_streams      : Dict[int, StreamCamera] = {}
_streams_lock : threading.Lock          = threading.Lock()


def adicionar_stream(cam_id: int, nome: str, url: str,
                     gravar: bool = False, sensibilidade: int = 50,
                     gravacoes_path: str = '', thumbs_path: str = '',
                     zona_pontos: str = '', transporte: str = 'auto'):
    with _streams_lock:
        if cam_id in _streams:
            _streams[cam_id].encerrar()
        _streams[cam_id] = StreamCamera(
            cam_id, nome, url, gravar, sensibilidade,
            gravacoes_path, thumbs_path, zona_pontos, transporte
        )
        logger.info(
            f"Stream {cam_id} adicionado · transporte={transporte}."
        )


def remover_stream(cam_id: int):
    with _streams_lock:
        if cam_id in _streams:
            _streams[cam_id].encerrar()
            del _streams[cam_id]
            gc.collect()
            logger.info(f"Stream {cam_id} removido.")


def atualizar_config_stream(cam_id: int, gravar: bool, sensibilidade: int):
    with _streams_lock:
        if cam_id in _streams:
            _streams[cam_id].atualizar_config(gravar, sensibilidade)


def atualizar_zona_stream(cam_id: int, zona_pontos: str):
    with _streams_lock:
        if cam_id in _streams:
            _streams[cam_id].atualizar_zona(zona_pontos)


def status_stream(cam_id: int) -> bool:
    with _streams_lock:
        return _streams[cam_id].online if cam_id in _streams else False


def listar_streams() -> list:
    with _streams_lock:
        return [
            {
                'id'        : cid,
                'nome'      : s.nome,
                'online'    : s.online,
                'gravando'  : s._recording,
                'noturno'   : s._modo_noturno,
                'tem_zona'  : s._zona_mascara is not None,
                'falhas'    : s._falhas,
                'transporte': s.transporte,
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
    from urllib.parse import quote

    time.sleep(2)
    logger.info("Iniciando streams salvos...")
    try:
        with get_conexao(db_path) as conn:
            rows = conn.execute(
                """SELECT id, nome, ip, porta, usuario, senha,
                          gravar, sensibilidade, zona_pontos,
                          COALESCE(rtsp_path,  '')     as rtsp_path,
                          COALESCE(transporte, 'auto') as transporte
                   FROM cameras WHERE ativa = 1"""
            ).fetchall()

        for row in rows:
            rtsp_path   = row['rtsp_path']  or ''
            transporte  = row['transporte'] or 'auto'
            usuario_raw = row['usuario']    or ''
            senha_raw   = row['senha']      or ''

            # safe='%' → preserva % já codificado na senha
            usuario_enc = quote(usuario_raw, safe='%') if usuario_raw else ''
            senha_enc   = quote(senha_raw,   safe='%') if senha_raw   else ''

            if usuario_enc and senha_enc:
                auth = f"{usuario_enc}:{senha_enc}@"
            elif usuario_enc:
                auth = f"{usuario_enc}@"
            else:
                auth = ""

            # Monta URL
            if rtsp_path:
                path = rtsp_path.strip().lstrip('/')
                url  = f"rtsp://{auth}{row['ip']}/{path}"
            else:
                url = (
                    f"rtsp://{auth}{row['ip']}:{row['porta']}/live/ch00_0"
                )

            logger.info(
                f"[Cam {row['id']}] '{row['nome']}' · "
                f"transporte={transporte} · "
                f"rtsp://{usuario_enc or 'anon'}:***@"
                f"{row['ip']}"
                f"{'/' + path if rtsp_path else ':' + str(row['porta'])}"
            )

            adicionar_stream(
                row['id'], row['nome'], url,
                bool(row['gravar']), row['sensibilidade'],
                gravacoes_path, thumbs_path,
                row['zona_pontos'] or '',
                transporte
            )
            time.sleep(0.5)

        logger.info(f"{len(rows)} câmera(s) iniciada(s).")
    except Exception as e:
        logger.error(f"Erro ao iniciar streams: {e}", exc_info=True)


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
        time.sleep(0.04)


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
