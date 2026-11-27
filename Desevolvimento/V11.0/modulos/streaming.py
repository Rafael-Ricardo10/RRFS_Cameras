#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Módulo de Streaming
Captura RTSP · Detecção de Movimento com Zona (ROI) · Dia/Noite · Gravação
+ Inteligência Artificial: Detecção de Objetos (YOLOv8) + Reconhecimento Facial
+ Integração PTZ: pausa detecção de movimento durante deslocamento da câmera
Otimizado para 24h/7 sem vazamento de memória

CORREÇÃO (lógica de gravação por IA):
Antes, quando modo_gravacao='movimento' (padrão), o sistema gravava em
QUALQUER movimento detectado pelo MOG2, mesmo que o usuário tivesse
marcado categorias específicas (pessoa/veículo/animal) nos checkboxes
de detecção da IA. Ou seja, a IA ficava "decorativa": só mostrava os
rótulos no vídeo, mas não influenciava a decisão de gravar.

Agora, com IA ativa e ao menos uma categoria marcada, o modo "Qualquer
Movimento" passa a exigir que a IA realmente confirme uma das categorias
marcadas para iniciar a gravação — não basta mais a imagem só mudar.
Se nenhuma categoria estiver marcada, mantém o comportamento clássico
(qualquer movimento), pois não há o que a IA filtrar.

CORREÇÃO v2 (ÁUDIO NAS GRAVAÇÕES):
O OpenCV (cv2.VideoCapture/cv2.VideoWriter), usado para capturar e
gravar os frames de vídeo, NÃO tem suporte a áudio — trabalha apenas
com imagem. Por isso, mesmo câmeras com microfone, as gravações
sempre saíam mudas.

Agora, ao iniciar uma gravação, um processo FFmpeg paralelo é aberto
(segunda conexão RTSP com a câmera) só para capturar o fluxo de
áudio em um arquivo .aac temporário. Ao finalizar a gravação, esse
áudio é mixado (merge) com o vídeo processado na mesma etapa de
conversão para H.264. Se a câmera não tiver áudio disponível ou não
aceitar a segunda conexão RTSP, o sistema cai automaticamente para
gravação sem áudio (fallback silencioso, não quebra a gravação).

CORREÇÃO v2 (POSIÇÃO DO WATERMARK):
O nome do sistema ("RRFS Cameras") e a data/hora apareciam em duas
linhas empilhadas no canto SUPERIOR esquerdo do vídeo. Agora aparecem
em uma única linha, lado a lado, no canto INFERIOR esquerdo, com um
fundo semi-transparente atrás para manter a legibilidade sobre
qualquer cena.

NOVO v3 (ORGANIZAÇÃO DE GRAVAÇÕES POR CÂMERA/DATA):
Antes, todos os vídeos e thumbnails de TODAS as câmeras ficavam soltos
juntos dentro de uma única pasta (dados/gravacoes/ e dados/thumbs/),
misturados e sem nenhuma separação — dificultando localizar os eventos
de uma câmera/dia específico à medida que o sistema acumula gravações.

Agora a estrutura passa a ser:

    dados/gravacoes/<NomeDaCamera>/<AAAA-MM-DD>/arquivo.mp4
    dados/thumbs/<NomeDaCamera>/<AAAA-MM-DD>/arquivo.jpg

A pasta da DATA só é criada no exato momento em que o primeiro evento
daquele dia é efetivamente gravado (dentro de _iniciar_gravacao) — ou
seja, dias sem nenhum evento NÃO geram pasta vazia. O nome da pasta da
câmera é o nome cadastrado, "higienizado" para ser um nome de pasta
válido (caracteres como / \ : * ? " < > | são substituídos por "_").

Vídeos gravados ANTES desta atualização (soltos na raiz das pastas)
continuam funcionando normalmente para reprodução/download — o sistema
sempre usa o caminho exato salvo no banco de dados, seja no formato
antigo (sem subpastas) ou no novo (com subpastas).
"""

import cv2
import re
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
from modulos           import ia_deteccao

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

# ── Configurações de IA ────────────────────────────────────────────────────
IA_MIN_INTERVALO      = 1.0
COOLDOWN_SALVAR_ROSTO = 20

# ── Watermark ─────────────────────────────────────────────────────────────
WATERMARK_TITULO = "RRFS Cameras"

# ── Áudio (captura paralela via FFmpeg) ────────────────────────────────────
AUDIO_TAMANHO_MINIMO_BYTES = 2048   # abaixo disso, considera "sem áudio"
AUDIO_STOP_TIMEOUT         = 5      # segundos aguardando o ffmpeg de áudio fechar

# ── NOVO (v3): organização de pastas por Câmera/Data ───────────────────────
# Caracteres não permitidos (ou problemáticos) em nomes de pasta.
_CARACTERES_INVALIDOS_PASTA = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

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
    modo dia/noite adaptativo, Inteligência Artificial (objetos + facial),
    controle de estado PTZ e gravação de eventos com watermark + áudio,
    organizados em subpastas por Câmera/Data.
    Thread-safe · sem vazamento de memória.
    """

    def __init__(self, cam_id: int, nome: str, url: str,
                 gravar: bool = False, sensibilidade: int = 50,
                 gravacoes_path: str = '', thumbs_path: str = '',
                 rostos_path: str = '',
                 zona_pontos: str = '', transporte: str = 'auto',
                 ia_ativa: bool = False,
                 detectar_pessoa: bool = True,
                 detectar_veiculo: bool = False,
                 detectar_animal: bool = False,
                 reconhecimento_facial: bool = False,
                 confianca_minima: float = 0.5,
                 modo_gravacao: str = 'movimento',
                 pessoa_alvo_id: Optional[int] = None):

        self.cam_id         = cam_id
        self.nome           = nome
        self.url            = url
        self.online         = False
        self.ativo          = True
        self.gravar         = gravar
        self.sensibilidade  = sensibilidade
        self.gravacoes_path = gravacoes_path
        self.thumbs_path    = thumbs_path
        self.rostos_path    = rostos_path
        self.transporte     = transporte

        self.db_path = os.path.join(
            os.path.dirname(self.gravacoes_path), 'config', 'sistema.db'
        )

        # ── NOVO (v3): nome de pasta seguro para esta câmera ─────────────
        # Calculado uma vez e mantido estável durante a vida deste objeto.
        self._pasta_camera = self._nome_pasta_camera()

        # ── Configuração de Inteligência Artificial ─────────────────────
        self.ia_ativa              = ia_ativa
        self.detectar_pessoa       = detectar_pessoa
        self.detectar_veiculo      = detectar_veiculo
        self.detectar_animal       = detectar_animal
        self.reconhecimento_facial = reconhecimento_facial
        self.confianca_minima      = confianca_minima
        self.modo_gravacao         = modo_gravacao
        self.pessoa_alvo_id        = pessoa_alvo_id

        self._ia_ultima_execucao = 0.0
        self._ia_ultimo_resultado: Optional[dict] = None
        self._rostos_cooldown: Dict[str, float] = {}

        # ── Estado de movimento físico da câmera PTZ ─────────────────────
        # Quando True, a detecção de movimento (MOG2) é pausada, pois a
        # câmera está fisicamente se movendo (patrulha ou controle manual)
        # e o frame inteiro mudaria, gerando falsos positivos.
        self.ptz_em_movimento = False

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
        self._evento_rel_dir       = ''   # NOVO (v3): "NomeCamera/AAAA-MM-DD"
        self._evento_frames        = 0
        self._evento_objetos       : set = set()
        self._evento_pessoas       : set = set()
        self._evento_inicio_dt     : Optional[datetime] = None

        # ── NOVO (v2): captura de áudio em paralelo (FFmpeg) ─────────────
        # O OpenCV não grava áudio, então mantemos um processo FFmpeg
        # separado, aberto só durante o período de gravação, conectado
        # à mesma URL RTSP, extraindo somente o áudio para um arquivo
        # temporário. Esse arquivo é mixado com o vídeo ao finalizar.
        self._audio_process   : Optional[subprocess.Popen] = None
        self._audio_temp_path : str = ''

        # Debug
        self._frame_count    = 0
        self._last_log_frame = 0

        self._iniciar_thread()
        logger.info(
            f"[Cam {self.cam_id}] Inicializada · gravar={self.gravar} "
            f"sensib={self.sensibilidade}% "
            f"transporte={self.transporte} "
            f"zona={'SIM' if self._zona_pontos else 'NÃO'} "
            f"IA={'ATIVA' if self.ia_ativa else 'inativa'} "
            f"modo_gravacao={self.modo_gravacao} "
            f"pasta='{self._pasta_camera}' "
            f"ffmpeg={'OK' if _FFMPEG_OK else 'AUSENTE'}"
        )

    # ── NOVO (v3): organização de pastas ──────────────────────────────────
    def _nome_pasta_camera(self) -> str:
        """
        Gera um nome de pasta seguro para o sistema de arquivos a partir
        do nome da câmera (ex: 'Entrada / Garagem' -> 'Entrada _ Garagem').
        Se o nome ficar vazio após a limpeza, usa 'camera_<id>' como
        fallback para nunca quebrar a gravação.
        """
        nome = (self.nome or '').strip()
        nome = _CARACTERES_INVALIDOS_PASTA.sub('_', nome)
        nome = nome.strip('. ')
        if not nome:
            nome = f"camera_{self.cam_id}"
        return nome

    @staticmethod
    def _juntar_rel(*partes: str) -> str:
        """
        Junta partes em um caminho relativo usando '/' (independe do SO),
        usado para montar o valor salvo no banco (arquivo_video/
        arquivo_thumb) de forma consistente.
        """
        limpos = [p.strip('/\\') for p in partes if p]
        return '/'.join(limpos)

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

    # ── Controle de estado PTZ ──────────────────────────────────────────
    def marcar_ptz_movimento(self, em_movimento: bool):
        """
        Chamado pelo módulo PTZ sempre que a câmera começa/termina de
        se mover fisicamente (patrulha ou joystick manual).
        Ao parar de mover, reseta o MOG2 para reaprender a nova cena
        e evita que o próprio deslocamento seja interpretado como
        "movimento detectado".
        """
        self.ptz_em_movimento = em_movimento
        if not em_movimento:
            self._resetar_mog2()
            logger.debug(
                f"[Cam {self.cam_id}] PTZ parou de mover · "
                f"MOG2 resetado (nova posição)."
            )

    # ── Watermark ─────────────────────────────────────────────────────────
    def _desenhar_watermark(self, frame: np.ndarray) -> np.ndarray:
        """
        Desenha "RRFS Cameras" + data/hora em uma ÚNICA LINHA, lado a
        lado, no canto INFERIOR ESQUERDO do frame — com um fundo
        semi-transparente atrás para manter a legibilidade sobre
        qualquer cena (clara ou escura).
        """
        agora = datetime.now()
        texto = f"{WATERMARK_TITULO}   {agora.strftime('%d/%m/%Y %H:%M:%S')}"

        fonte      = cv2.FONT_HERSHEY_SIMPLEX
        escala     = 0.48
        espessura  = 1
        cor_txt    = (255, 255, 255)
        cor_sombra = (0, 0, 0)

        h, w = frame.shape[:2]
        margem_x = 8
        margem_y = 10

        (tw, th), baseline = cv2.getTextSize(texto, fonte, escala, espessura)

        x = margem_x
        y = h - margem_y  # próximo da borda inferior

        # ── Fundo semi-transparente para legibilidade sobre qualquer cena ──
        x0, y0 = max(0, x - 5), max(0, y - th - 6)
        x1, y1 = min(w, x + tw + 5), min(h, y + baseline + 4)
        try:
            overlay = frame.copy()
            cv2.rectangle(overlay, (x0, y0), (x1, y1), (0, 0, 0), -1)
            cv2.addWeighted(overlay, 0.45, frame, 0.55, 0, frame)
            del overlay
        except Exception:
            pass

        cv2.putText(frame, texto, (x + 1, y + 1),
                    fonte, escala, cor_sombra, espessura + 1, cv2.LINE_AA)
        cv2.putText(frame, texto, (x, y),
                    fonte, escala, cor_txt, espessura, cv2.LINE_AA)

        return frame

    # ── Desenho de Detecções de IA ──────────────────────────────────────────
    def _desenhar_deteccoes(self, frame: np.ndarray,
                            resultado_ia: Optional[dict]) -> np.ndarray:
        if not resultado_ia:
            return frame
        try:
            pessoas_por_bbox = {
                tuple(p['bbox']): p
                for p in resultado_ia.get('pessoas_identificadas', [])
            }

            for obj in resultado_ia.get('objetos', []):
                x1, y1, x2, y2 = obj['bbox']
                bbox_key = tuple(obj['bbox'])

                if obj['tipo'] == 'pessoa' and bbox_key in pessoas_por_bbox:
                    info  = pessoas_por_bbox[bbox_key]
                    nome  = info.get('nome', 'Desconhecido')
                    conhecido = bool(info.get('pessoa_id'))
                    cor   = (0, 220, 0) if conhecido else (0, 140, 255)
                    label = nome
                    if info.get('confianca'):
                        label += f" {info['confianca']:.0f}%"
                else:
                    cor   = (255, 170, 0)
                    label = f"{obj['tipo']} {obj['confianca']*100:.0f}%"

                cv2.rectangle(frame, (x1, y1), (x2, y2), cor, 2)
                (tw, th), _ = cv2.getTextSize(
                    label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1
                )
                ty = max(th + 8, y1)
                cv2.rectangle(
                    frame, (x1, ty - th - 8), (x1 + tw + 6, ty), cor, -1
                )
                cv2.putText(
                    frame, label, (x1 + 3, ty - 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA
                )
        except Exception as e:
            logger.error(f"[Cam {self.cam_id}] Erro ao desenhar detecções: {e}")
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

                    orig_h, orig_w = frame.shape[:2]

                    frame_res = cv2.resize(
                        frame, (FRAME_WIDTH, FRAME_HEIGHT)
                    )

                    # ── Detecção de Movimento (MOG2) ───────────────────────
                    # Pausa detecção enquanto a câmera PTZ se move
                    movimento = False
                    if (self.gravar or self.ia_ativa) and \
                       not self.ptz_em_movimento:
                        movimento = self._detectar_movimento(frame_res)

                    # ── Inteligência Artificial (throttled) ────────────────
                    resultado_ia = None
                    if self.ia_ativa and movimento and \
                       not self.ptz_em_movimento:
                        agora = time.time()
                        if agora - self._ia_ultima_execucao >= IA_MIN_INTERVALO:
                            resultado_bruto = self._processar_ia(frame)
                            self._ia_ultima_execucao = agora
                            resultado_ia = self._escalar_resultado(
                                resultado_bruto, orig_w, orig_h
                            )
                            self._ia_ultimo_resultado = resultado_ia
                            del resultado_bruto

                            if resultado_ia.get('pessoas_identificadas'):
                                self._processar_rostos_detectados(
                                    resultado_ia['pessoas_identificadas']
                                )
                        else:
                            resultado_ia = self._ia_ultimo_resultado

                    deve_gravar = self._avaliar_gravacao(movimento, resultado_ia)

                    # ── Gravação ────────────────────────────────────────────
                    if self.gravar:
                        frame_grav = frame_res.copy()
                        if resultado_ia:
                            self._desenhar_deteccoes(frame_grav, resultado_ia)
                        self._desenhar_watermark(frame_grav)
                        self._gerenciar_gravacao(
                            frame_grav, deve_gravar, resultado_ia
                        )
                        del frame_grav

                    # ── Stream web ──────────────────────────────────────────
                    if count % FRAME_SKIP == 0:
                        frame_vis = frame_res.copy()
                        self._desenhar_zona(frame_vis)
                        if resultado_ia:
                            self._desenhar_deteccoes(frame_vis, resultado_ia)
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
                    if resultado_ia:
                        del resultado_ia

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

    # ── Pipeline de IA ────────────────────────────────────────────────────
    def _processar_ia(self, frame: np.ndarray) -> dict:
        try:
            config = {
                'detectar_pessoa'      : self.detectar_pessoa,
                'detectar_veiculo'     : self.detectar_veiculo,
                'detectar_animal'      : self.detectar_animal,
                'reconhecimento_facial': self.reconhecimento_facial,
                'confianca_minima'     : self.confianca_minima,
            }
            return ia_deteccao.processar_frame_completo(frame, config)
        except Exception as e:
            logger.error(f"[Cam {self.cam_id}] Erro no pipeline de IA: {e}")
            return {
                'objetos': [], 'pessoas_identificadas': [],
                'tem_pessoa': False, 'tem_veiculo': False, 'tem_animal': False
            }

    def _escalar_resultado(self, resultado: dict,
                           orig_w: int, orig_h: int) -> dict:
        if not resultado:
            return resultado

        sx = FRAME_WIDTH  / orig_w if orig_w else 1
        sy = FRAME_HEIGHT / orig_h if orig_h else 1

        def _escala_bbox(bbox):
            x1, y1, x2, y2 = bbox
            return (int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy))

        return {
            'objetos': [
                {**o, 'bbox': _escala_bbox(o['bbox'])}
                for o in resultado.get('objetos', [])
            ],
            'pessoas_identificadas': [
                {**p, 'bbox': _escala_bbox(p['bbox'])}
                for p in resultado.get('pessoas_identificadas', [])
            ],
            'tem_pessoa' : resultado.get('tem_pessoa', False),
            'tem_veiculo': resultado.get('tem_veiculo', False),
            'tem_animal' : resultado.get('tem_animal', False),
        }

    # ═══════════════════════════════════════════════════════════════════
    #  CORREÇÃO: decisão de gravação respeitando as categorias
    #  de IA marcadas pelo usuário, mesmo no modo "Qualquer Movimento".
    # ═══════════════════════════════════════════════════════════════════
    def _avaliar_gravacao(self, movimento: bool,
                          resultado_ia: Optional[dict]) -> bool:
        """
        Decide se o frame atual deve iniciar/manter uma gravação.

        Regras:
        - Sem movimento (MOG2) → nunca grava.
        - IA desativada → grava em qualquer movimento (modo clássico).
        - IA ativada:
            • modo 'pessoa_especifica' → só grava ao reconhecer a pessoa
              cadastrada selecionada (ou qualquer pessoa cadastrada, se
              nenhuma foi escolhida especificamente).
            • modo 'movimento' (padrão) → se o usuário marcou ao menos
              UMA categoria nos checkboxes (Pessoas/Veículos/Animais),
              a gravação só ocorre quando a IA CONFIRMAR uma delas —
              não basta mais a imagem simplesmente mudar de pixel.
              Se nenhuma categoria estiver marcada, mantém o
              comportamento clássico (qualquer movimento), pois não há
              o que a IA filtrar.
            • valores legados ('pessoa', 'veiculo', 'animal') — mantidos
              apenas por retrocompatibilidade com câmeras configuradas
              antes desta correção; equivalem a marcar só aquela
              categoria.
        """
        if not movimento:
            return False

        if not self.ia_ativa:
            return True

        modo = self.modo_gravacao

        # ── Pessoa específica ────────────────────────────────────────
        if modo == 'pessoa_especifica':
            if resultado_ia is None:
                return False
            if not self.pessoa_alvo_id:
                return resultado_ia.get('tem_pessoa', False)
            for p in resultado_ia.get('pessoas_identificadas', []):
                if p.get('pessoa_id') == self.pessoa_alvo_id:
                    return True
            return False

        # ── Valores legados (retrocompatibilidade) ──────────────────
        if modo == 'pessoa':
            return bool(resultado_ia) and resultado_ia.get('tem_pessoa', False)
        if modo == 'veiculo':
            return bool(resultado_ia) and resultado_ia.get('tem_veiculo', False)
        if modo == 'animal':
            return bool(resultado_ia) and resultado_ia.get('tem_animal', False)

        # ── Modo 'movimento' (padrão) ────────────────────────────────
        categorias_marcadas = (self.detectar_pessoa or
                               self.detectar_veiculo or
                               self.detectar_animal)

        if not categorias_marcadas:
            # Nenhuma categoria marcada: não há o que a IA filtrar,
            # mantém o comportamento clássico de "qualquer movimento".
            return True

        if resultado_ia is None:
            # IA ainda não produziu um resultado válido para este
            # instante (throttle) → aguarda, em vez de gravar "às
            # cegas" baseado só em diferença de pixels.
            return False

        condicoes = []
        if self.detectar_pessoa:
            condicoes.append(resultado_ia.get('tem_pessoa', False))
        if self.detectar_veiculo:
            condicoes.append(resultado_ia.get('tem_veiculo', False))
        if self.detectar_animal:
            condicoes.append(resultado_ia.get('tem_animal', False))

        return any(condicoes)

    def _processar_rostos_detectados(self, pessoas_identificadas: list):
        agora = time.time()
        for p in pessoas_identificadas:
            chave  = p.get('nome', 'Desconhecido')
            ultimo = self._rostos_cooldown.get(chave, 0)
            if agora - ultimo < COOLDOWN_SALVAR_ROSTO:
                continue
            self._rostos_cooldown[chave] = agora

            ok = ia_deteccao.salvar_rosto_detectado(
                self.db_path, self.rostos_path,
                self.cam_id, None, p
            )
            if ok:
                logger.info(
                    f"[Cam {self.cam_id}] 👤 Rosto salvo na galeria: {chave}"
                )

            if 'foto_rosto' in p:
                del p['foto_rosto']

    # ── Conexão ───────────────────────────────────────────────────────────
    def _tentar_conectar_com(self, protocolo: str) -> Optional[cv2.VideoCapture]:
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
            else:
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
                return None

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
                return None

            logger.info(
                f"[Cam {self.cam_id}] ✓ Conectado via {protocolo.upper()}."
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
        modo = (self.transporte or 'auto').lower().strip()

        if modo == 'tcp':
            cap = self._tentar_conectar_com('tcp')
            if cap is None:
                self._falhas += 1
            return cap

        elif modo == 'udp':
            cap = self._tentar_conectar_com('udp')
            if cap is None:
                self._falhas += 1
            return cap

        else:
            cap = self._tentar_conectar_com('tcp')
            if cap is not None:
                return cap

            logger.info(f"[Cam {self.cam_id}] TCP falhou, tentando UDP...")
            cap = self._tentar_conectar_com('udp')
            if cap is not None:
                return cap

            self._falhas += 1
            return None

    def _aguardar_reconexao(self):
        espera = RECONECT_WAIT * 6 if self._falhas >= MAX_FALHAS \
                 else RECONECT_WAIT
        logger.info(f"[Cam {self.cam_id}] Reconectando em {espera}s...")
        time.sleep(espera)

    # ── Zona visual ───────────────────────────────────────────────────────
    def _desenhar_zona(self, frame: np.ndarray) -> np.ndarray:
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
        try:
            if self._mog2 is None:
                self._criar_mog2()

            gray       = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            luma_media = float(np.mean(gray))
            self._modo_noturno = (luma_media < NIGHT_LUMA_THRESHOLD)

            gray_blur = cv2.GaussianBlur(gray, (5, 5), 0)
            fg_mask   = self._mog2.apply(gray_blur)

            self._mog2_warmup += 1
            if self._mog2_warmup <= 30:
                del gray, gray_blur, fg_mask
                return False

            if self._zona_mascara is not None:
                fg_mask = cv2.bitwise_and(
                    fg_mask, fg_mask, mask=self._zona_mascara
                )

            kernel  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
            fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN,  kernel)
            fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_CLOSE, kernel)
            fg_mask = cv2.dilate(fg_mask, kernel, iterations=2)

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
    def _gerenciar_gravacao(self, frame: np.ndarray, deve_gravar: bool,
                            resultado_ia: Optional[dict] = None):
        frame_copy = frame.copy()
        self._frames_buffer.append(frame_copy)
        if len(self._frames_buffer) > MOTION_BUFFER_BEFORE:
            old = self._frames_buffer.pop(0)
            del old

        if deve_gravar:
            self._frames_sem_movimento = 0
            if not self._recording:
                self._iniciar_gravacao()
        else:
            if self._recording:
                self._frames_sem_movimento += 1
                if self._frames_sem_movimento > MOTION_BUFFER_AFTER:
                    self._finalizar_gravacao()

        if self._recording and resultado_ia:
            for obj in resultado_ia.get('objetos', []):
                self._evento_objetos.add(obj['tipo'])
            for p in resultado_ia.get('pessoas_identificadas', []):
                self._evento_pessoas.add(p.get('nome', 'Desconhecido'))

        if self._recording and self._video_writer is not None:
            self._video_writer.write(frame)
            self._evento_frames += 1

    def _iniciar_gravacao(self):
        """
        Inicia uma nova gravação de evento.

        NOVO (v3): o arquivo é criado dentro de uma subpasta
        <PastaDaCamera>/<AAAA-MM-DD> — criada agora, pela primeira vez,
        somente neste momento (quando um evento REAL começa a ser
        gravado). Dias sem nenhum evento nunca geram essa pasta.
        """
        try:
            agora    = datetime.now()
            ts       = agora.strftime("%Y%m%d_%H%M%S")
            modo_tag = "noite" if self._modo_noturno else "dia"
            filename = f"cam{self.cam_id}_{ts}_{modo_tag}.mp4"
            data_str = agora.strftime("%Y-%m-%d")

            # ── NOVO (v3): cria (se preciso) a subpasta Câmera/Data ──────
            dir_evento = os.path.join(
                self.gravacoes_path, self._pasta_camera, data_str
            )
            os.makedirs(dir_evento, exist_ok=True)

            filepath = os.path.join(dir_evento, filename)

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

            for f in self._frames_buffer:
                self._video_writer.write(f)

            self._recording            = True
            self._evento_path          = filepath
            self._evento_rel_dir       = self._juntar_rel(
                self._pasta_camera, data_str
            )
            self._evento_frames        = len(self._frames_buffer)
            self._frames_sem_movimento = 0
            self._evento_objetos       = set()
            self._evento_pessoas       = set()
            self._evento_inicio_dt     = agora

            # ── NOVO (v2): inicia captura de áudio em paralelo ───────────
            self._iniciar_captura_audio()

            modo_str  = "🌙 noturno" if self._modo_noturno else "☀️ diurno"
            zona_str  = "com zona"   if self._zona_mascara is not None \
                        else "sem zona"
            ia_str    = f"IA:{self.modo_gravacao}" if self.ia_ativa else "sem IA"
            audio_str = "c/ áudio" if self._audio_process else "sem áudio"
            logger.info(
                f"[Cam {self.cam_id}] ► GRAVAÇÃO INICIADA "
                f"[{modo_str}][{zona_str}][{ia_str}][{audio_str}]: "
                f"{self._evento_rel_dir}/{filename}"
            )
        except Exception as e:
            logger.error(
                f"[Cam {self.cam_id}] Erro ao iniciar gravação: {e}",
                exc_info=True
            )
            self._recording = False

    # ═══════════════════════════════════════════════════════════════════
    #  NOVO (v2): CAPTURA DE ÁUDIO EM PARALELO
    # ═══════════════════════════════════════════════════════════════════
    def _iniciar_captura_audio(self):
        """
        Inicia um processo FFmpeg separado, conectado à MESMA câmera
        (segunda conexão RTSP), apenas para capturar o fluxo de áudio
        durante o período da gravação — já que o OpenCV (usado para
        capturar os frames de vídeo) não tem suporte a áudio.

        O áudio é salvo em um arquivo .aac temporário (na mesma
        subpasta do vídeo), que será mixado (merge) com o vídeo já
        processado (zona/detecções/watermark) ao final da gravação, na
        mesma etapa de conversão para H.264.

        Se a câmera não tiver áudio disponível, não aceitar a segunda
        conexão RTSP, ou o FFmpeg não estiver instalado, a gravação
        simplesmente segue sem áudio — isso NUNCA impede a gravação
        de vídeo de funcionar.
        """
        self._audio_process   = None
        self._audio_temp_path = ''

        if not _FFMPEG_OK or not self._evento_path:
            return

        try:
            audio_path = self._evento_path.rsplit('.', 1)[0] + '_audio.aac'
            rtsp_transport = 'udp' if (self.transporte or '').lower() == 'udp' \
                             else 'tcp'

            cmd = [
                _FFMPEG_PATH, '-y',
                '-rtsp_transport', rtsp_transport,
                '-i', self.url,
                '-vn',                 # ignora vídeo, só áudio
                '-acodec', 'aac',
                '-ar', '44100',
                '-ac', '1',
                '-loglevel', 'error',
                audio_path
            ]
            self._audio_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL
            )
            self._audio_temp_path = audio_path

        except Exception as e:
            logger.warning(
                f"[Cam {self.cam_id}] Não foi possível iniciar captura de "
                f"áudio (gravação seguirá sem áudio): {e}"
            )
            self._audio_process   = None
            self._audio_temp_path = ''

    def _parar_captura_audio(self):
        """Encerra o processo FFmpeg de áudio de forma segura."""
        if self._audio_process is None:
            return
        try:
            self._audio_process.terminate()
            try:
                self._audio_process.wait(timeout=AUDIO_STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                self._audio_process.kill()
                self._audio_process.wait(timeout=3)
        except Exception as e:
            logger.debug(
                f"[Cam {self.cam_id}] Erro ao parar captura de áudio: {e}"
            )
        finally:
            self._audio_process = None

    def _remover_audio_temp(self):
        """Remove o arquivo .aac temporário após ele já ter sido usado."""
        if self._audio_temp_path and os.path.exists(self._audio_temp_path):
            try:
                os.remove(self._audio_temp_path)
            except Exception:
                pass
        self._audio_temp_path = ''

    def _converter_h264_ffmpeg(self, filepath: str) -> bool:
        """
        Converte o vídeo gravado (mp4v) para H.264 e, se houver um
        áudio capturado em paralelo válido, faz o MUX (mixagem) do
        áudio junto nessa mesma etapa — evitando reprocessar o vídeo
        duas vezes.
        """
        if not _FFMPEG_OK:
            return False

        filepath_tmp = filepath + '.tmp.mp4'

        tem_audio = bool(self._audio_temp_path) and \
            os.path.exists(self._audio_temp_path) and \
            os.path.getsize(self._audio_temp_path) > AUDIO_TAMANHO_MINIMO_BYTES

        try:
            cmd = [_FFMPEG_PATH, '-y', '-i', filepath]
            if tem_audio:
                cmd += ['-i', self._audio_temp_path]

            cmd += [
                '-c:v', 'libx264', '-preset', 'ultrafast',
                '-crf', '23', '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart',
            ]

            if tem_audio:
                # -shortest corta no final do stream mais curto (vídeo
                # ou áudio), evitando trilhas "sobrando" sem par.
                cmd += ['-c:a', 'aac', '-b:a', '128k', '-shortest']
            else:
                cmd += ['-an']

            cmd += [filepath_tmp]

            logger.info(
                f"[Cam {self.cam_id}] Convertendo → H.264"
                f"{' + áudio' if tem_audio else ' (sem áudio)'}..."
            )
            result = subprocess.run(
                cmd, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=90
            )

            if result.returncode != 0:
                erro = result.stderr.decode(errors='ignore')[-300:]
                logger.error(f"[Cam {self.cam_id}] FFmpeg erro: {erro}")
                if os.path.exists(filepath_tmp):
                    os.remove(filepath_tmp)
                if tem_audio:
                    logger.warning(
                        f"[Cam {self.cam_id}] Tentando novamente sem "
                        f"áudio (fallback de segurança)..."
                    )
                    return self._converter_h264_sem_audio(
                        filepath, filepath_tmp
                    )
                return False

            if not os.path.exists(filepath_tmp) or \
               os.path.getsize(filepath_tmp) < 10 * 1024:
                if os.path.exists(filepath_tmp):
                    os.remove(filepath_tmp)
                return False

            os.replace(filepath_tmp, filepath)
            logger.info(
                f"[Cam {self.cam_id}] ✓ H.264"
                f"{' c/ áudio' if tem_audio else ' (sem áudio)'}: "
                f"{os.path.getsize(filepath)/1024/1024:.1f} MB"
            )
            return True

        except subprocess.TimeoutExpired:
            logger.error(f"[Cam {self.cam_id}] Timeout FFmpeg.")
            if os.path.exists(filepath_tmp):
                os.remove(filepath_tmp)
            if tem_audio:
                return self._converter_h264_sem_audio(filepath, filepath_tmp)
            return False
        except Exception as e:
            logger.error(f"[Cam {self.cam_id}] Erro FFmpeg: {e}")
            if os.path.exists(filepath_tmp):
                os.remove(filepath_tmp)
            return False

    def _converter_h264_sem_audio(self, filepath: str,
                                  filepath_tmp: str) -> bool:
        """
        Fallback de segurança: usado quando a tentativa de mixar o
        áudio falha por algum motivo (ex: arquivo .aac corrompido).
        Garante que o vídeo não seja perdido, mesmo que fique sem som.
        """
        try:
            cmd = [
                _FFMPEG_PATH, '-y', '-i', filepath,
                '-c:v', 'libx264', '-preset', 'ultrafast',
                '-crf', '23', '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart', '-an', filepath_tmp
            ]
            result = subprocess.run(
                cmd, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=60
            )
            if result.returncode != 0:
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
                f"[Cam {self.cam_id}] ✓ H.264 (fallback sem áudio): "
                f"{os.path.getsize(filepath)/1024/1024:.1f} MB"
            )
            return True
        except Exception as e:
            logger.error(
                f"[Cam {self.cam_id}] Erro no fallback sem áudio: {e}"
            )
            if os.path.exists(filepath_tmp):
                os.remove(filepath_tmp)
            return False

    def _finalizar_gravacao(self):
        """
        Finaliza a gravação atual: converte para H.264 (+ áudio se
        disponível), gera o thumbnail e registra o evento no banco.

        NOVO (v3): o thumbnail é salvo na MESMA subpasta relativa
        (Câmera/Data) dentro de dados/thumbs/, e tanto o caminho do
        vídeo quanto o do thumbnail são gravados no banco já incluindo
        essa subpasta (ex: "Entrada/2024-05-27/cam1_....mp4").
        """
        if not self._recording:
            return
        try:
            if self._video_writer:
                self._video_writer.release()
                self._video_writer = None

            # ── NOVO (v2): para a captura de áudio ANTES de converter ────
            # Garante que o arquivo .aac esteja completo e fechado antes
            # de tentar usá-lo no merge com o vídeo.
            self._parar_captura_audio()

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

            self._converter_h264_ffmpeg(filepath)

            if os.path.exists(filepath):
                tamanho = os.path.getsize(filepath)

            # ── NOVO (v3): thumbnail na mesma subpasta Câmera/Data ───────
            thumb_name = os.path.basename(filepath).replace('.mp4', '.jpg')
            if self._evento_rel_dir:
                thumb_dir = os.path.join(self.thumbs_path, self._evento_rel_dir)
            else:
                thumb_dir = self.thumbs_path
            os.makedirs(thumb_dir, exist_ok=True)
            thumb_path = os.path.join(thumb_dir, thumb_name)
            self._gerar_thumbnail(filepath, thumb_path)

            duracao = max(1, int(self._evento_frames / MOTION_FPS))

            objetos_json = json.dumps(sorted(self._evento_objetos)) \
                           if self._evento_objetos else ''
            pessoas_str  = ', '.join(sorted(self._evento_pessoas)) \
                           if self._evento_pessoas else ''

            # ── NOVO (v3): caminhos relativos salvos no banco já incluem
            # a subpasta Câmera/Data (ex: "Entrada/2024-05-27/arq.mp4") ──
            if self._evento_rel_dir:
                arquivo_video_rel = self._juntar_rel(
                    self._evento_rel_dir, os.path.basename(filepath)
                )
                arquivo_thumb_rel = self._juntar_rel(
                    self._evento_rel_dir, thumb_name
                )
            else:
                arquivo_video_rel = os.path.basename(filepath)
                arquivo_thumb_rel = thumb_name

            with get_conexao(self.db_path) as conn:
                cursor = conn.execute(
                    """INSERT INTO eventos
                       (camera_id, arquivo_video, arquivo_thumb,
                        duracao, tamanho_bytes,
                        objetos_detectados, pessoa_reconhecida)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (self.cam_id, arquivo_video_rel, arquivo_thumb_rel,
                     duracao, tamanho, objetos_json, pessoas_str)
                )
                evento_id = cursor.lastrowid

                if self._evento_inicio_dt:
                    inicio_str = self._evento_inicio_dt.strftime(
                        '%Y-%m-%d %H:%M:%S'
                    )
                    conn.execute(
                        "UPDATE rostos_detectados SET evento_id=? "
                        "WHERE camera_id=? AND evento_id IS NULL "
                        "AND data_deteccao >= ?",
                        (evento_id, self.cam_id, inicio_str)
                    )

            tag_extra = f" · {pessoas_str}" if pessoas_str else ''
            logger.info(
                f"[Cam {self.cam_id}] ■ SALVO: "
                f"{arquivo_video_rel} · {duracao}s · "
                f"{tamanho/1024/1024:.1f} MB{tag_extra}"
            )
        except Exception as e:
            logger.error(
                f"[Cam {self.cam_id}] Erro ao finalizar: {e}",
                exc_info=True
            )
        finally:
            # ── NOVO (v2): limpa o .aac temporário (sucesso ou falha) ────
            self._remover_audio_temp()
            self._recording            = False
            self._evento_path          = ''
            self._evento_rel_dir       = ''
            self._evento_frames        = 0
            self._frames_sem_movimento = 0
            self._evento_objetos       = set()
            self._evento_pessoas       = set()
            self._evento_inicio_dt     = None

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
        if not gravar and self._recording:
            self._finalizar_gravacao()

    def atualizar_config_ia(self, ia_ativa: bool, detectar_pessoa: bool,
                            detectar_veiculo: bool, detectar_animal: bool,
                            reconhecimento_facial: bool,
                            confianca_minima: float, modo_gravacao: str,
                            pessoa_alvo_id: Optional[int]):
        self.ia_ativa              = ia_ativa
        self.detectar_pessoa       = detectar_pessoa
        self.detectar_veiculo      = detectar_veiculo
        self.detectar_animal       = detectar_animal
        self.reconhecimento_facial = reconhecimento_facial
        self.confianca_minima      = confianca_minima
        self.modo_gravacao         = modo_gravacao
        self.pessoa_alvo_id        = pessoa_alvo_id
        self._ia_ultima_execucao   = 0.0
        self._ia_ultimo_resultado  = None

    def encerrar(self):
        logger.info(f"[Cam {self.cam_id}] Encerrando '{self.nome}'...")
        self.ativo  = False
        self.online = False
        self._finalizar_gravacao()
        # ── NOVO (v2): segurança extra contra processo de áudio órfão ────
        self._parar_captura_audio()
        self._remover_audio_temp()
        with self._lock:
            self._frame_jpeg = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=8)
        self._frames_buffer.clear()
        self._rostos_cooldown.clear()
        self._ia_ultimo_resultado = None
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
                     rostos_path: str = '',
                     zona_pontos: str = '', transporte: str = 'auto',
                     ia_ativa: bool = False,
                     detectar_pessoa: bool = True,
                     detectar_veiculo: bool = False,
                     detectar_animal: bool = False,
                     reconhecimento_facial: bool = False,
                     confianca_minima: float = 0.5,
                     modo_gravacao: str = 'movimento',
                     pessoa_alvo_id: Optional[int] = None):
    with _streams_lock:
        if cam_id in _streams:
            _streams[cam_id].encerrar()
        _streams[cam_id] = StreamCamera(
            cam_id, nome, url, gravar, sensibilidade,
            gravacoes_path, thumbs_path, rostos_path,
            zona_pontos, transporte,
            ia_ativa, detectar_pessoa, detectar_veiculo,
            detectar_animal, reconhecimento_facial,
            confianca_minima, modo_gravacao, pessoa_alvo_id
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


def atualizar_zona_stream(cam_id: int, zona_pontos: str):
    with _streams_lock:
        if cam_id in _streams:
            _streams[cam_id].atualizar_zona(zona_pontos)


def atualizar_config_ia_stream(cam_id: int, **kwargs):
    with _streams_lock:
        if cam_id in _streams:
            _streams[cam_id].atualizar_config_ia(**kwargs)


# ── Integração PTZ ────────────────────────────────────────────────────────
def notificar_ptz_estado(cam_id: int, em_movimento: bool):
    """Chamado pelo módulo PTZ ao iniciar/parar movimento físico."""
    with _streams_lock:
        if cam_id in _streams:
            _streams[cam_id].marcar_ptz_movimento(em_movimento)


def obter_frame_cv2(cam_id: int) -> Optional[np.ndarray]:
    """
    Decodifica o último frame JPEG da câmera para um array numpy.
    Usado pela rotina de auto-calibração PTZ (análise visual de
    deslocamento), reaproveitando o frame já capturado pelo stream
    em vez de abrir uma nova conexão RTSP.
    """
    with _streams_lock:
        stream = _streams.get(cam_id)
        frame_bytes = stream.get_frame() if stream else None
    if not frame_bytes:
        return None
    try:
        arr = np.frombuffer(frame_bytes, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)
    except Exception as e:
        logger.error(f"Erro ao decodificar frame cam {cam_id}: {e}")
        return None


def status_stream(cam_id: int) -> bool:
    with _streams_lock:
        return _streams[cam_id].online if cam_id in _streams else False


def listar_streams() -> list:
    with _streams_lock:
        agora = time.time()
        resultado = []
        for cid, s in _streams.items():
            deteccao_recente = (agora - s._ia_ultima_execucao) < 5
            ultimo = s._ia_ultimo_resultado if deteccao_recente else None

            resultado.append({
                'id'                 : cid,
                'nome'               : s.nome,
                'online'             : s.online,
                'gravando'           : s._recording,
                'noturno'            : s._modo_noturno,
                'tem_zona'           : s._zona_mascara is not None,
                'falhas'             : s._falhas,
                'transporte'         : s.transporte,
                'ia_ativa'           : s.ia_ativa,
                'ptz_em_movimento'   : s.ptz_em_movimento,
                'objetos_detectados' : [
                    o['tipo'] for o in (ultimo or {}).get('objetos', [])
                ],
                'pessoas_detectadas' : [
                    {'nome': p.get('nome'), 'confianca': p.get('confianca')}
                    for p in (ultimo or {}).get('pessoas_identificadas', [])
                ],
            })
        return resultado


def encerrar_todos_streams():
    with _streams_lock:
        for s in _streams.values():
            s.encerrar()
        _streams.clear()
    gc.collect()
    logger.info("Todos os streams encerrados.")


def iniciar_streams_salvos(db_path: str,
                            gravacoes_path: str,
                            thumbs_path: str,
                            rostos_path: str):
    from urllib.parse import quote

    time.sleep(2)
    logger.info("Iniciando streams salvos...")

    try:
        motor = ia_deteccao.obter_motor_ia()
        motor.recarregar_pessoas(db_path)
    except Exception as e:
        logger.error(f"Erro ao preparar motor de IA: {e}")

    try:
        with get_conexao(db_path) as conn:
            rows = conn.execute(
                """SELECT id, nome, ip, porta, usuario, senha,
                          gravar, sensibilidade, zona_pontos,
                          COALESCE(rtsp_path,  '')     as rtsp_path,
                          COALESCE(transporte, 'auto') as transporte,
                          COALESCE(ia_ativa, 0)               as ia_ativa,
                          COALESCE(detectar_pessoa, 1)        as detectar_pessoa,
                          COALESCE(detectar_veiculo, 0)       as detectar_veiculo,
                          COALESCE(detectar_animal, 0)        as detectar_animal,
                          COALESCE(reconhecimento_facial, 0)  as reconhecimento_facial,
                          COALESCE(confianca_minima, 0.5)     as confianca_minima,
                          COALESCE(modo_gravacao, 'movimento') as modo_gravacao,
                          pessoa_alvo_id
                   FROM cameras WHERE ativa = 1"""
            ).fetchall()

        for row in rows:
            rtsp_path   = row['rtsp_path']  or ''
            transporte  = row['transporte'] or 'auto'
            usuario_raw = row['usuario']    or ''
            senha_raw   = row['senha']      or ''

            usuario_enc = quote(usuario_raw, safe='%') if usuario_raw else ''
            senha_enc   = quote(senha_raw,   safe='%') if senha_raw   else ''

            if usuario_enc and senha_enc:
                auth = f"{usuario_enc}:{senha_enc}@"
            elif usuario_enc:
                auth = f"{usuario_enc}@"
            else:
                auth = ""

            if rtsp_path:
                path = rtsp_path.strip().lstrip('/')
                url  = f"rtsp://{auth}{row['ip']}/{path}"
            else:
                url = f"rtsp://{auth}{row['ip']}:{row['porta']}/live/ch00_0"

            adicionar_stream(
                row['id'], row['nome'], url,
                bool(row['gravar']), row['sensibilidade'],
                gravacoes_path, thumbs_path, rostos_path,
                row['zona_pontos'] or '', transporte,
                bool(row['ia_ativa']),
                bool(row['detectar_pessoa']),
                bool(row['detectar_veiculo']),
                bool(row['detectar_animal']),
                bool(row['reconhecimento_facial']),
                float(row['confianca_minima']),
                row['modo_gravacao'],
                row['pessoa_alvo_id']
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
