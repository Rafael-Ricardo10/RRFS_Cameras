#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Módulo de Controle PTZ (Pan-Tilt-Zoom)
Protocolo ONVIF · Auto-Calibração Visual · Presets · Patrulha Automática

v4 - CORREÇÕES DE LENTIDÃO/LAG (round 2): ver histórico anterior.
v5 - RETORNO AUTOMÁTICO À POSIÇÃO INICIAL (auto-tracking).
v6 - FALLBACK DE PRESET VIA POSIÇÃO ABSOLUTA (GetStatus/AbsoluteMove)
     quando a câmera não suporta SetPreset/GotoPreset nativo.
     + Tracking não "briga" mais com a patrulha ao perder o alvo.
v7 - CONTROLE 100% POR SOFTWARE (dead-reckoning / "odômetro de tempo").
v8 - PATRULHA CONTÍNUA + TRACKING SUAVE.

v9 - CORREÇÃO DE VELOCIDADE/RESPONSIVIDADE DO TRACKING:

Dois problemas identificados no uso real:

  1. CADA comando ContinuousMove/Stop abria uma conexão TCP NOVA
     (header "Connection: close"), com handshake completo a cada
     envio. No tracking (reavaliação a cada 0.25s), boa parte do
     ciclo era consumida só em overhead de rede antes mesmo do motor
     da câmera começar a girar — especialmente em câmeras baratas com
     pilha ONVIF lenta. Agora o controller mantém uma conexão
     persistente (keep-alive) reaproveitada entre comandos, caindo
     para reconexão automática apenas se a câmera fechar a conexão
     ou o envio falhar.

  2. A curva de velocidade de correção do tracking (_nivel) só
     chegava perto da velocidade máxima quando o alvo estava quase
     saindo pela borda da imagem (erro ≈ 0.5). Para alvos que
     atravessam rápido o campo de visão sem chegar literalmente na
     borda, a velocidade enviada ficava perto do piso mínimo — baixa
     demais para acompanhar. A curva agora sobe em raiz quadrada
     (ganho rápido já com pequeno deslocamento do centro) e o piso
     mínimo subiu de 0.35 para 0.5.

  Além disso, pan/tilt/zoom agora são arredondados a 2 casas decimais
  antes de enviar — isso faz o controller reconhecer corretamente
  comandos "iguais" consecutivos (ruído de ponto flutuante antes
  impedia esse reaproveitamento) e evita reenvios desnecessários que
  interrompem o movimento em andamento.

IMPORTANTE: se mesmo após esta correção a velocidade continuar baixa,
use o endpoint de diagnóstico já existente:
    POST /api/ptz/<cam_id>/medir_velocidade
Se ele concluir que a câmera "gira numa velocidade fixa via ONVIF",
é limitação de firmware/hardware da câmera, não do software.
"""

import os
import gc
import cv2
import time
import uuid
import socket
import logging
import threading
import numpy as np
from typing import Dict, List, Optional, Tuple

from flask         import (Blueprint, render_template, request,
                           redirect, url_for, flash, current_app, jsonify)
from flask_login   import login_required, current_user

from modulos.database import get_conexao

logger  = logging.getLogger('RRFS.ptz')
ptz_bp  = Blueprint('ptz', __name__)


# ── Cliente ONVIF "cru" (estilo ZoneMinder Netcat ONVIF) ─────────────────
import re
import base64
import hashlib
from datetime import datetime, timezone
from xml.sax.saxutils import escape as _xml_escape

_ONVIF_OK = True  # não depende mais de biblioteca externa


def ptz_disponivel() -> bool:
    return _ONVIF_OK


# ── Parâmetros de calibração ────────────────────────────────────────────
TEST_VELOCIDADE        = 0.5
TEST_DURACAO_SEGUNDOS  = 1.2
TEMPO_ESTABILIZAR      = 0.4
RESPOSTA_MINIMA        = 0.10
DESLOCAMENTO_MINIMO_PX = 2.5

# ── Parâmetros de patrulha ───────────────────────────────────────────────
TEMPO_BUFFER_MOVIMENTO          = 4.0   # buffer de segurança após comando
                                         # de movimento (modo clássico, com
                                         # paradas em cada preset)
TEMPO_BUFFER_MOVIMENTO_CONTINUO = 1.5   # buffer reduzido entre presets
                                         # quando a patrulha é contínua
TEMPO_ESTABILIZAR_CONTINUO      = 0.6   # pausa mínima em cada ponto no modo
                                         # contínuo (só estabiliza a imagem,
                                         # NÃO é o tempo_parada configurado)

# ── Parâmetros de resiliência / protocolo ────────────────────────────────
TIMEOUT_PRECHECK_PORTA  = 1.5
COOLDOWN_RECONEXAO      = 20
TIMEOUT_HTTP            = 2.5
MAX_TENTATIVAS_CMD      = 2
AUTO_STOP_SEGUNDOS      = 2.0
TIMEOUT_CAMERA_MOVIMENTO = 3.0

TEMPO_MAXIMO_MOVIMENTO_VIRTUAL = 25.0

# Caminhos testados automaticamente (o primeiro que responder é usado)
CAMINHOS_ONVIF = [
    '/onvif/deviceio_service',   # Yoosee (igual ao ZoneMinder)
    '/onvif/ptz_service',
    '/onvif/device_service',
    '/onvif/PTZ',
    '/onvif/services',
]
MODOS_AUTH = ('digest', 'texto', 'nenhum')

NS_PTZ    = 'http://www.onvif.org/ver20/ptz/wsdl'
NS_MEDIA  = 'http://www.onvif.org/ver10/media/wsdl'
NS_SCHEMA = 'http://www.onvif.org/ver10/schema'


class PTZController:
    """Controle PTZ via SOAP/ONVIF direto (sem zeep)."""

    def __init__(self, cam_id: int, ip: str, porta: int,
                 usuario: str, senha: str,
                 caminho: str = '/onvif/deviceio_service',
                 tipo: str = 'netcat_onvif',
                 parada: str = 'auto'):
        self.cam_id  = cam_id
        self.ip      = ip
        self.porta   = porta
        self.usuario = usuario or ''
        self.senha   = senha or ''

        self._lock           = threading.RLock()
        self._conectado      = False
        self._profile_token  = 'PROFILE_000'
        self._path           = CAMINHOS_ONVIF[0]
        self._auth_idx       = 0

        self._ultima_tentativa = 0.0
        self._ultimo_erro      = ''
        self._timer_stop       = None

        # ── Configuração escolhida pelo usuário (tela PTZ) ──────────────
        caminho = (caminho or '').strip() or CAMINHOS_ONVIF[0]
        if not caminho.startswith('/'):
            caminho = '/' + caminho
        self._caminho_cfg = caminho
        self._tipo        = tipo if tipo in ('netcat_onvif', 'auto') \
                            else 'netcat_onvif'
        self._path        = caminho
        self._parada_modo = parada if parada in ('stop', 'zero') else None

        self._moveu       = False
        self._ultimo_mov  = None    # (pan, tilt, zoom, timestamp)
        self._parar_req_ts = 0.0

        self._stop_flag_lock = threading.Lock()

        # ── NOVO (v9): conexão ONVIF persistente (keep-alive) ───────────
        # Evita abrir/fechar um socket TCP novo a cada comando (grande
        # fonte de latência em câmeras com pilha ONVIF lenta). Reutiliza
        # a mesma conexão enquanto ela se mantiver saudável; reconecta
        # automaticamente em caso de falha.
        self._sock       : Optional[socket.socket] = None
        self._sock_lock   = threading.Lock()

        # ── Escala real de velocidade da câmera ─────────────────────────
        self._pan_max          = 1.0
        self._tilt_max         = 1.0
        self._zoom_max         = 1.0
        self._escala_detectada = False

        # ── NOVO (v7): odômetro de posição virtual (dead-reckoning) ─────
        self._virtual_pan  = 0.0
        self._virtual_tilt = 0.0

        # NOVO (v7): flags "aprendidas" sobre o que esta câmera suporta.
        self._suporta_preset: Optional[bool] = None
        self._suporta_status: Optional[bool] = None

    # ── Baixo nível ─────────────────────────────────────────────────────
    def _porta_responde(self) -> bool:
        try:
            with socket.create_connection(
                (self.ip, self.porta), timeout=TIMEOUT_PRECHECK_PORTA
            ):
                return True
        except Exception:
            return False

    def _cabecalho_seguranca(self, modo: str) -> str:
        if modo == 'nenhum' or not self.usuario:
            return ''
        ns_sec = ('http://docs.oasis-open.org/wss/2004/01/'
                  'oasis-200401-wss-wssecurity-secext-1.0.xsd')
        ns_utl = ('http://docs.oasis-open.org/wss/2004/01/'
                  'oasis-200401-wss-wssecurity-utility-1.0.xsd')
        tp = ('http://docs.oasis-open.org/wss/2004/01/'
              'oasis-200401-wss-username-token-profile-1.0')
        usr = _xml_escape(self.usuario)

        if modo == 'texto':
            return (
                f'<Security xmlns="{ns_sec}"><UsernameToken>'
                f'<Username>{usr}</Username>'
                f'<Password Type="{tp}#PasswordText">'
                f'{_xml_escape(self.senha)}</Password>'
                f'</UsernameToken></Security>'
            )

        nonce   = os.urandom(16)
        created = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        digest  = base64.b64encode(
            hashlib.sha1(nonce + created.encode() +
                         self.senha.encode()).digest()
        ).decode()
        return (
            f'<Security xmlns="{ns_sec}"><UsernameToken>'
            f'<Username>{usr}</Username>'
            f'<Password Type="{tp}#PasswordDigest">{digest}</Password>'
            f'<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
            f'oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
            f'{base64.b64encode(nonce).decode()}</Nonce>'
            f'<Created xmlns="{ns_utl}">{created}</Created>'
            f'</UsernameToken></Security>'
        )

    # ── NOVO (v9): gerenciamento de conexão persistente ──────────────────
    def _obter_socket_persistente(self) -> socket.socket:
        """Retorna o socket ativo, criando uma nova conexão se necessário."""
        with self._sock_lock:
            if self._sock is None:
                s = socket.create_connection(
                    (self.ip, self.porta), timeout=TIMEOUT_HTTP
                )
                s.settimeout(TIMEOUT_HTTP)
                self._sock = s
            return self._sock

    def _fechar_socket_persistente(self):
        with self._sock_lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None

    @staticmethod
    def _ler_resposta_http(sock_obj: socket.socket) -> bytes:
        """Lê a resposta HTTP completa de um socket já conectado."""
        partes = []
        while True:
            try:
                c = sock_obj.recv(65536)
            except socket.timeout:
                break
            if not c:
                break
            partes.append(c)
            raw = b''.join(partes)
            if b'\r\n\r\n' in raw:
                cab, _, corpo_r = raw.partition(b'\r\n\r\n')
                m = re.search(rb'content-length:\s*(\d+)', cab, re.I)
                if m and len(corpo_r) >= int(m.group(1)):
                    break
                if raw.endswith(b'0\r\n\r\n'):
                    break
        return b''.join(partes)

    def _post_raw(self, path: str, corpo: str, modo: str):
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
            f'<s:Header>{self._cabecalho_seguranca(modo)}</s:Header>'
            f'<s:Body>{corpo}</s:Body></s:Envelope>'
        )
        dados = envelope.encode('utf-8')

        # ── NOVO (v9): "Connection: keep-alive" em vez de "close" ───────
        # Mantém a conexão aberta para reaproveitamento pelos próximos
        # comandos — elimina o custo de handshake TCP a cada envio.
        req = (
            f'POST {path} HTTP/1.1\r\n'
            f'Host: {self.ip}:{self.porta}\r\n'
            f'Content-Type: application/soap+xml; charset=utf-8\r\n'
            f'Content-Length: {len(dados)}\r\n'
            f'Connection: keep-alive\r\n\r\n'
        ).encode('utf-8') + dados

        ultima_excecao: Optional[Exception] = None

        # Tenta até 2 vezes: 1ª usando a conexão persistente (se existir),
        # 2ª forçando uma conexão nova caso a primeira tenha falhado
        # (câmera pode ter fechado a conexão por timeout do lado dela).
        for tentativa in (1, 2):
            try:
                sock_obj = self._obter_socket_persistente()
                sock_obj.sendall(req)
                raw = self._ler_resposta_http(sock_obj)

                if not raw:
                    raise ConnectionError(
                        'Câmera fechou a conexão sem responder'
                    )

                # Se a câmera avisou que vai fechar a conexão dela,
                # descartamos nosso lado também para não reutilizar um
                # socket morto na próxima chamada.
                if re.search(rb'(?im)^connection:\s*close', raw):
                    self._fechar_socket_persistente()

                m = re.match(rb'HTTP/\d\.\d\s+(\d+)', raw)
                status = int(m.group(1)) if m else 0
                return status, raw.decode('utf-8', errors='ignore')

            except Exception as e:
                ultima_excecao = e
                self._fechar_socket_persistente()
                if tentativa == 2:
                    raise

        # Nunca deveria chegar aqui, mas por segurança:
        raise ultima_excecao or RuntimeError('Falha desconhecida ao enviar comando ONVIF')

    @staticmethod
    def _falha_auth(status: int, texto: str) -> bool:
        if status in (401, 403):
            return True
        return bool(re.search(
            r'notauthorized|not authorized|sender not authorized',
            texto, re.I
        ))

    @staticmethod
    def _tem_fault(texto: str) -> bool:
        return bool(re.search(r'<[\w\-]*:?Fault[\s>]', texto))

    def _enviar(self, path: str, corpo: str):
        """Envia tentando os modos de autenticação até um funcionar."""
        for i in range(len(MODOS_AUTH)):
            idx  = (self._auth_idx + i) % len(MODOS_AUTH)
            modo = MODOS_AUTH[idx]
            status, texto = self._post_raw(path, corpo, modo)
            if self._falha_auth(status, texto):
                continue
            self._auth_idx = idx
            return status, texto
        raise RuntimeError(
            'Autenticação ONVIF recusada. Confira usuário e senha ONVIF '
            '(na Yoosee costumam ser admin + a senha da câmera).'
        )

    def _chamar(self, corpo: str) -> str:
        status, texto = self._enviar(self._path, corpo)
        if status >= 400 or status == 0 or self._tem_fault(texto):
            m = re.search(r'<[\w\-:]*Text[^>]*>([^<]+)<', texto)
            motivo = m.group(1) if m else texto[:150].replace('\n', ' ')
            raise RuntimeError(f'Câmera respondeu erro (HTTP {status}): {motivo}')
        return texto

    # ── Conexão ─────────────────────────────────────────────────────────
    def conectar(self) -> bool:
        with self._lock:
            if not self._porta_responde():
                self._ultimo_erro = (
                    f"Porta {self.porta} fechada/sem resposta em "
                    f"{self.ip} — verifique IP e porta ONVIF."
                )
                logger.error(f"[PTZ {self.cam_id}] {self._ultimo_erro}")
                self._conectado = False
                return False

            # Garante conexão persistente limpa antes de (re)conectar.
            self._fechar_socket_persistente()

            corpo = f'<GetProfiles xmlns="{NS_MEDIA}"/>'
            if self._tipo == 'auto':
                candidatos = [self._caminho_cfg] + [
                    c for c in CAMINHOS_ONVIF if c != self._caminho_cfg
                ]
            else:
                candidatos = [self._caminho_cfg]
            for path in candidatos:
                try:
                    status, texto = self._enviar(path, corpo)
                except Exception as e:
                    logger.debug(f"[PTZ {self.cam_id}] {path}: {e}")
                    continue
                if status >= 400 or self._tem_fault(texto):
                    continue
                m = re.search(
                    r'<[\w\-:]*Profiles[^>]*\btoken="([^"]+)"', texto
                )
                if m:
                    self._path          = path
                    self._profile_token = m.group(1)
                    self._conectado     = True
                    self._ultimo_erro   = ''
                    logger.info(
                        f"[PTZ {self.cam_id}] ✓ ONVIF OK "
                        f"{self.ip}:{self.porta}{path} "
                        f"perfil={self._profile_token} "
                        f"auth={MODOS_AUTH[self._auth_idx]}"
                    )
                    self._detectar_espaco_velocidade()
                    return True

            self._path          = self._caminho_cfg
            self._profile_token = 'PROFILE_000'
            self._conectado     = True
            self._ultimo_erro   = ''
            logger.warning(
                f"[PTZ {self.cam_id}] Não consegui listar perfis; usando "
                f"{self._path} e perfil {self._profile_token}."
            )
            self._detectar_espaco_velocidade()
            return True

    def _detectar_espaco_velocidade(self):
        """Pergunta à câmera a faixa real de velocidade do ContinuousMove."""
        try:
            corpo_cfg = f'<GetConfigurations xmlns="{NS_PTZ}"/>'
            texto_cfg = self._chamar(corpo_cfg)
            m_token = re.search(
                r'<[\w\-:]*PTZConfiguration[^>]*\btoken="([^"]+)"', texto_cfg
            )
            if not m_token:
                logger.debug(
                    f"[PTZ {self.cam_id}] GetConfigurations não retornou "
                    f"token; mantendo escala padrão ±1.0."
                )
                return
            config_token = m_token.group(1)

            corpo_opt = (
                f'<GetConfigurationOptions xmlns="{NS_PTZ}">'
                f'<ConfigurationToken>{_xml_escape(config_token)}'
                f'</ConfigurationToken></GetConfigurationOptions>'
            )
            texto_opt = self._chamar(corpo_opt)

            mudou = False

            m_pt = re.search(
                r'ContinuousPanTiltVelocitySpace.*?'
                r'XRange[^>]*>\s*<[\w:]*Min[^>]*>([\-\d.eE]+)</[\w:]*Min>\s*'
                r'<[\w:]*Max[^>]*>([\-\d.eE]+)</[\w:]*Max>.*?'
                r'YRange[^>]*>\s*<[\w:]*Min[^>]*>([\-\d.eE]+)</[\w:]*Min>\s*'
                r'<[\w:]*Max[^>]*>([\-\d.eE]+)</[\w:]*Max>',
                texto_opt, re.S
            )
            if m_pt:
                pan_min, pan_max, tilt_min, tilt_max = map(float, m_pt.groups())
                novo_pan_max  = max(abs(pan_min),  abs(pan_max))  or 1.0
                novo_tilt_max = max(abs(tilt_min), abs(tilt_max)) or 1.0
                if abs(novo_pan_max - self._pan_max) > 0.01 or \
                   abs(novo_tilt_max - self._tilt_max) > 0.01:
                    mudou = True
                self._pan_max  = novo_pan_max
                self._tilt_max = novo_tilt_max

            m_z = re.search(
                r'ContinuousZoomVelocitySpace.*?'
                r'XRange[^>]*>\s*<[\w:]*Min[^>]*>([\-\d.eE]+)</[\w:]*Min>\s*'
                r'<[\w:]*Max[^>]*>([\-\d.eE]+)</[\w:]*Max>',
                texto_opt, re.S
            )
            if m_z:
                zoom_min, zoom_max = map(float, m_z.groups())
                self._zoom_max = max(abs(zoom_min), abs(zoom_max)) or 1.0

            self._escala_detectada = True

            if self._pan_max > 1.01 or self._tilt_max > 1.01 or \
               self._zoom_max > 1.01:
                logger.info(
                    f"[PTZ {self.cam_id}] ⚡ Câmera usa escala de "
                    f"velocidade PRÓPRIA (fora do padrão ONVIF ±1.0): "
                    f"pan=±{self._pan_max:g} tilt=±{self._tilt_max:g} "
                    f"zoom=±{self._zoom_max:g}. Ajustando automaticamente."
                )
            elif mudou:
                logger.info(
                    f"[PTZ {self.cam_id}] Escala de velocidade confirmada "
                    f"como padrão ONVIF (±1.0)."
                )

        except Exception as e:
            logger.debug(
                f"[PTZ {self.cam_id}] Não foi possível obter o espaço de "
                f"velocidade ONVIF (mantendo padrão ±1.0): {e}"
            )

    def _garantir_conectado(self) -> bool:
        if self._conectado:
            return True
        agora = time.time()
        if agora - self._ultima_tentativa < COOLDOWN_RECONEXAO:
            return False
        self._ultima_tentativa = agora
        return self.conectar()

    def erro_atual(self) -> str:
        return self._ultimo_erro

    def _executar(self, operacao, descricao: str,
                  tentativas: Optional[int] = None,
                  reconectar: bool = True):
        n = tentativas or MAX_TENTATIVAS_CMD
        with self._lock:
            if not self._garantir_conectado():
                raise RuntimeError(self._ultimo_erro or 'PTZ não conectado')

            ultimo = None
            for tentativa in range(1, n + 1):
                try:
                    return operacao()
                except Exception as e:
                    ultimo = e
                    logger.warning(
                        f"[PTZ {self.cam_id}] {descricao} falhou "
                        f"(tentativa {tentativa}/{n}): {e}"
                    )
                    if tentativa < n:
                        time.sleep(0.2)
                    if reconectar and n >= 3 and tentativa == 2:
                        self._conectado = False
                        self.conectar()

            self._ultimo_erro = f'{descricao} falhou: {ultimo}'
            raise RuntimeError(self._ultimo_erro)

    # ── Auto stop de segurança ──────────────────────────────────────────
    def _cancelar_auto_stop(self):
        if self._timer_stop is not None:
            self._timer_stop.cancel()
            self._timer_stop = None

    def _agendar_auto_stop(self):
        self._cancelar_auto_stop()
        t = threading.Timer(AUTO_STOP_SEGUNDOS, self._auto_stop)
        t.daemon = True
        self._timer_stop = t
        t.start()

    def _auto_stop(self):
        parou = True
        try:
            parou = self.parar()
        except Exception as e:
            logger.warning(f"[PTZ {self.cam_id}] Auto-stop falhou: {e}")
        if parou:
            try:
                from modulos.streaming import notificar_ptz_estado
                notificar_ptz_estado(self.cam_id, False)
            except Exception:
                pass

    # ── NOVO (v7): odômetro de posição virtual ──────────────────────────
    def _flush_virtual(self, agora: Optional[float] = None):
        if self._ultimo_mov is None:
            return
        pan, tilt, zoom, t0 = self._ultimo_mov
        agora = agora if agora is not None else time.time()
        dt = max(0.0, agora - t0)
        self._virtual_pan  += pan  * dt
        self._virtual_tilt += tilt * dt

    def obter_posicao_virtual(self) -> Tuple[float, float]:
        with self._lock:
            self._flush_virtual()
            return self._virtual_pan, self._virtual_tilt

    def estimar_tempo_virtual(self, pan_alvo: float, tilt_alvo: float,
                              velocidade: float = 0.5) -> float:
        with self._lock:
            self._flush_virtual()
            delta_pan  = pan_alvo  - self._virtual_pan
            delta_tilt = tilt_alvo - self._virtual_tilt
        maior = max(abs(delta_pan), abs(delta_tilt))
        if maior <= 0:
            return 0.0
        v = max(0.1, min(1.0, velocidade))
        return min(TEMPO_MAXIMO_MOVIMENTO_VIRTUAL, maior / v)

    def mover_para_virtual(self, pan_alvo: float, tilt_alvo: float,
                           velocidade: float = 0.5,
                           tolerancia: float = 0.05) -> bool:
        with self._lock:
            self._flush_virtual()
            delta_pan  = pan_alvo  - self._virtual_pan
            delta_tilt = tilt_alvo - self._virtual_tilt

        maior = max(abs(delta_pan), abs(delta_tilt))
        if maior < tolerancia:
            return True

        v = max(0.1, min(1.0, velocidade))
        tempo_total = min(TEMPO_MAXIMO_MOVIMENTO_VIRTUAL, maior / v)

        pan_speed  = (delta_pan  / maior) * v if maior > 0 else 0.0
        tilt_speed = (delta_tilt / maior) * v if maior > 0 else 0.0

        logger.info(
            f"[PTZ {self.cam_id}] 🧭 Movendo por tempo estimado "
            f"(posição virtual): Δpan={delta_pan:+.3f} Δtilt={delta_tilt:+.3f} "
            f"· {tempo_total:.1f}s"
        )

        try:
            self.mover_continuo(pan_speed, tilt_speed)
            time.sleep(tempo_total)
        finally:
            self.parar(forcar=True)

        with self._lock:
            self._virtual_pan  = pan_alvo
            self._virtual_tilt = tilt_alvo

        return True

    # ── Comandos ────────────────────────────────────────────────────────
    def mover_continuo(self, pan: float, tilt: float, zoom: float = 0.0,
                       t_req: Optional[float] = None):
        # ── NOVO (v9): arredondamento antes de comparar/enviar ──────────
        # Elimina reenvios causados por ruído de ponto flutuante entre
        # cálculos sucessivos do tracking (ex: 0.7123 vs 0.7124), que
        # antes faziam o controller achar que a velocidade "mudou" e
        # reenviar o comando sem necessidade, interrompendo levemente
        # o movimento em andamento a cada ciclo.
        pan  = round(max(-1.0, min(1.0, pan)),  2)
        tilt = round(max(-1.0, min(1.0, tilt)), 2)
        zoom = round(max(-1.0, min(1.0, zoom)), 2)

        with self._lock:
            if t_req is not None and self._parar_req_ts > t_req:
                return

            um = self._ultimo_mov
            if self._moveu and um and um[:3] == (pan, tilt, zoom):
                self._agendar_auto_stop()
                return

            self._flush_virtual()

            pan_env  = pan  * self._pan_max
            tilt_env = tilt * self._tilt_max
            zoom_env = zoom * self._zoom_max

            corpo = (
                f'<ContinuousMove xmlns="{NS_PTZ}">'
                f'<ProfileToken>{_xml_escape(self._profile_token)}</ProfileToken>'
                f'<Velocity>'
                f'<PanTilt x="{pan_env:.4f}" y="{tilt_env:.4f}" xmlns="{NS_SCHEMA}"/>'
                f'<Zoom x="{zoom_env:.4f}" xmlns="{NS_SCHEMA}"/>'
                f'</Velocity>'
                f'<Timeout>PT{TIMEOUT_CAMERA_MOVIMENTO:.0f}S</Timeout>'
                f'</ContinuousMove>'
            )
            self._executar(lambda: self._chamar(corpo), 'ContinuousMove',
                           tentativas=1, reconectar=False)
            self._moveu      = True
            self._ultimo_mov = (pan, tilt, zoom, time.time())
            self._agendar_auto_stop()

    def _parar_via_stop(self, tentativas: int = 1):
        corpo = (
            f'<Stop xmlns="{NS_PTZ}">'
            f'<ProfileToken>{_xml_escape(self._profile_token)}</ProfileToken>'
            f'<PanTilt>true</PanTilt><Zoom>true</Zoom></Stop>'
        )
        self._executar(lambda: self._chamar(corpo), 'Stop',
                       tentativas=tentativas, reconectar=False)

    def _parar_via_zero(self):
        corpo = (
            f'<ContinuousMove xmlns="{NS_PTZ}">'
            f'<ProfileToken>{_xml_escape(self._profile_token)}</ProfileToken>'
            f'<Velocity>'
            f'<PanTilt x="0" y="0" xmlns="{NS_SCHEMA}"/>'
            f'<Zoom x="0" xmlns="{NS_SCHEMA}"/>'
            f'</Velocity>'
            f'</ContinuousMove>'
        )
        self._executar(lambda: self._chamar(corpo), 'Stop(vel. zero)',
                       tentativas=1, reconectar=False)

    def parar(self, forcar: bool = False) -> bool:
        self._parar_req_ts = time.time()
        self._cancelar_auto_stop()

        if not self._stop_flag_lock.acquire(blocking=False):
            return True

        try:
            with self._lock:
                self._flush_virtual()

                if not self._moveu and not forcar:
                    return False

                modo = self._parada_modo
                if modo == 'zero':
                    self._parar_via_zero()
                elif modo == 'stop':
                    self._parar_via_stop(tentativas=1)
                else:
                    try:
                        self._parar_via_stop(tentativas=1)
                    except Exception:
                        logger.info(
                            f"[PTZ {self.cam_id}] Câmera não responde ao "
                            f"Stop oficial; usando velocidade zero daqui "
                            f"em diante."
                        )
                        self._parada_modo = 'zero'
                        self._parar_via_zero()

                self._moveu      = False
                self._ultimo_mov = None
                return True
        finally:
            self._stop_flag_lock.release()

    def set_preset(self, nome: str, token: Optional[str] = None) -> Optional[str]:
        token_xml = (
            f'<PresetToken>{_xml_escape(token)}</PresetToken>' if token else ''
        )
        corpo = (
            f'<SetPreset xmlns="{NS_PTZ}">'
            f'<ProfileToken>{_xml_escape(self._profile_token)}</ProfileToken>'
            f'{token_xml}'
            f'<PresetName>{_xml_escape(nome)}</PresetName></SetPreset>'
        )
        try:
            texto = self._executar(lambda: self._chamar(corpo), 'SetPreset')
        except Exception:
            self._suporta_preset = False
            raise
        self._suporta_preset = True
        m = re.search(r'<[\w\-:]*PresetToken[^>]*>([^<]+)<', texto)
        return m.group(1).strip() if m else token

    def goto_preset(self, token: str, velocidade: float = 0.5):
        v = max(0.1, min(1.0, velocidade))
        corpo = (
            f'<GotoPreset xmlns="{NS_PTZ}">'
            f'<ProfileToken>{_xml_escape(self._profile_token)}</ProfileToken>'
            f'<PresetToken>{_xml_escape(str(token))}</PresetToken>'
            f'<Speed><PanTilt x="{v:.2f}" y="{v:.2f}" xmlns="{NS_SCHEMA}"/>'
            f'<Zoom x="{v:.2f}" xmlns="{NS_SCHEMA}"/></Speed>'
            f'</GotoPreset>'
        )
        self._executar(lambda: self._chamar(corpo), 'GotoPreset')

    def remove_preset(self, token: str):
        corpo = (
            f'<RemovePreset xmlns="{NS_PTZ}">'
            f'<ProfileToken>{_xml_escape(self._profile_token)}</ProfileToken>'
            f'<PresetToken>{_xml_escape(str(token))}</PresetToken>'
            f'</RemovePreset>'
        )
        self._executar(lambda: self._chamar(corpo), 'RemovePreset')

    def obter_status(self) -> Optional[dict]:
        try:
            corpo = (
                f'<GetStatus xmlns="{NS_PTZ}">'
                f'<ProfileToken>{_xml_escape(self._profile_token)}</ProfileToken>'
                f'</GetStatus>'
            )
            texto = self._executar(lambda: self._chamar(corpo), 'GetStatus',
                                   tentativas=1, reconectar=False)

            m_pt = re.search(
                r'<[\w\-:]*PanTilt[^>]*\bx="([\-\d.eE]+)"[^>]*\by="([\-\d.eE]+)"',
                texto
            )
            if not m_pt:
                self._suporta_status = False
                return None

            m_z = re.search(
                r'<[\w\-:]*Zoom[^>]*\bx="([\-\d.eE]+)"', texto
            )
            pan, tilt = float(m_pt.group(1)), float(m_pt.group(2))
            zoom      = float(m_z.group(1)) if m_z else 0.0
            self._suporta_status = True
            return {'pan': pan, 'tilt': tilt, 'zoom': zoom}

        except Exception as e:
            self._suporta_status = False
            logger.debug(
                f"[PTZ {self.cam_id}] GetStatus indisponível "
                f"(câmera provavelmente não suporta): {e}"
            )
            return None

    def mover_absoluto(self, pan: float, tilt: float, zoom: float = 0.0,
                       velocidade: float = 0.5) -> bool:
        v = max(0.1, min(1.0, velocidade))
        corpo = (
            f'<AbsoluteMove xmlns="{NS_PTZ}">'
            f'<ProfileToken>{_xml_escape(self._profile_token)}</ProfileToken>'
            f'<Position>'
            f'<PanTilt x="{pan:.4f}" y="{tilt:.4f}" xmlns="{NS_SCHEMA}"/>'
            f'<Zoom x="{zoom:.4f}" xmlns="{NS_SCHEMA}"/>'
            f'</Position>'
            f'<Speed>'
            f'<PanTilt x="{v:.2f}" y="{v:.2f}" xmlns="{NS_SCHEMA}"/>'
            f'<Zoom x="{v:.2f}" xmlns="{NS_SCHEMA}"/>'
            f'</Speed>'
            f'</AbsoluteMove>'
        )
        try:
            self._executar(lambda: self._chamar(corpo), 'AbsoluteMove',
                           tentativas=1, reconectar=False)
            return True
        except Exception as e:
            logger.debug(
                f"[PTZ {self.cam_id}] AbsoluteMove indisponível "
                f"(câmera provavelmente não suporta): {e}"
            )
            return False


# ── Cache global de controllers (1 por câmera) ──────────────────────────
_controllers      : Dict[int, PTZController] = {}
_controllers_lock : threading.Lock           = threading.Lock()


def _carregar_config_camera(db_path: str, cam_id: int) -> Optional[dict]:
    try:
        with get_conexao(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM cameras WHERE id=?", (cam_id,)
            ).fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"Erro ao carregar config PTZ da câmera {cam_id}: {e}")
        return None


def obter_controller(db_path: str, cam_id: int) -> Optional[PTZController]:
    with _controllers_lock:
        if cam_id in _controllers:
            return _controllers[cam_id]

    config = _carregar_config_camera(db_path, cam_id)
    if not config or not config.get('ptz_ativo'):
        return None

    controller = PTZController(
        cam_id, config['ip'], config.get('onvif_porta') or 80,
        config.get('onvif_usuario', ''), config.get('onvif_senha', ''),
        caminho=config.get('onvif_caminho') or '/onvif/deviceio_service',
        tipo=config.get('onvif_tipo') or 'netcat_onvif',
        parada=config.get('onvif_parada') or 'auto'
    )
    with _controllers_lock:
        _controllers[cam_id] = controller
    return controller


def invalidar_controller(cam_id: int):
    with _controllers_lock:
        controller = _controllers.pop(cam_id, None)
    # NOVO (v9): fecha a conexão persistente ao invalidar o controller,
    # evitando deixar um socket órfão aberto.
    if controller is not None:
        try:
            controller._fechar_socket_persistente()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
#  AUTO-CALIBRAÇÃO VISUAL
# ═══════════════════════════════════════════════════════════════════════════
def _detectar_deslocamento(frame_a: np.ndarray, frame_b: np.ndarray):
    try:
        gray_a = cv2.cvtColor(frame_a, cv2.COLOR_BGR2GRAY).astype(np.float32)
        gray_b = cv2.cvtColor(frame_b, cv2.COLOR_BGR2GRAY).astype(np.float32)

        if gray_a.shape != gray_b.shape:
            gray_b = cv2.resize(gray_b, (gray_a.shape[1], gray_a.shape[0]))

        (dx, dy), resposta = cv2.phaseCorrelate(gray_a, gray_b)
        del gray_a, gray_b
        return float(dx), float(dy), float(resposta)
    except Exception as e:
        logger.error(f"Erro ao calcular deslocamento visual: {e}")
        return 0.0, 0.0, 0.0


def executar_calibracao(cam_id: int, controller: PTZController,
                        obter_frame_fn) -> dict:
    resultado = {
        'sucesso'       : False,
        'pan_detectado' : False,
        'tilt_detectado': False,
        'invert_pan'    : False,
        'invert_tilt'   : False,
        'mensagem'      : ''
    }

    if not controller._garantir_conectado():
        resultado['mensagem'] = (
            controller.erro_atual() or
            'Falha ao conectar via ONVIF. Verifique IP, porta, '
            'usuário e senha.'
        )
        return resultado

    mensagens = []

    try:
        frame_a = obter_frame_fn(cam_id)
        if frame_a is None:
            resultado['mensagem'] = 'Não foi possível capturar imagem da câmera.'
            return resultado

        controller.mover_continuo(TEST_VELOCIDADE, 0.0)
        time.sleep(TEST_DURACAO_SEGUNDOS)
        controller.parar()
        time.sleep(TEMPO_ESTABILIZAR)

        frame_b = obter_frame_fn(cam_id)
        dx, dy, resp = _detectar_deslocamento(frame_a, frame_b)

        if resp >= RESPOSTA_MINIMA and abs(dx) >= DESLOCAMENTO_MINIMO_PX:
            resultado['pan_detectado'] = True
            resultado['invert_pan'] = bool(dx > 0)

            controller.mover_continuo(-TEST_VELOCIDADE, 0.0)
            time.sleep(TEST_DURACAO_SEGUNDOS)
            controller.parar()
        else:
            mensagens.append(
                'Eixo PAN: movimento não confirmado visualmente '
                '(cena sem referências suficientes ou câmera não se moveu).'
            )

        del frame_a, frame_b
        time.sleep(0.5)
    except Exception as e:
        logger.error(f"[PTZ {cam_id}] Erro ao testar PAN: {e}")
        mensagens.append(f'Erro ao testar PAN: {e}')

    try:
        frame_c = obter_frame_fn(cam_id)
        if frame_c is None:
            resultado['mensagem'] = ' '.join(mensagens) or \
                'Não foi possível capturar imagem da câmera.'
            return resultado

        controller.mover_continuo(0.0, TEST_VELOCIDADE)
        time.sleep(TEST_DURACAO_SEGUNDOS)
        controller.parar()
        time.sleep(TEMPO_ESTABILIZAR)

        frame_d = obter_frame_fn(cam_id)
        dx2, dy2, resp2 = _detectar_deslocamento(frame_c, frame_d)

        if resp2 >= RESPOSTA_MINIMA and abs(dy2) >= DESLOCAMENTO_MINIMO_PX:
            resultado['tilt_detectado'] = True
            resultado['invert_tilt'] = bool(dy2 < 0)

            controller.mover_continuo(0.0, -TEST_VELOCIDADE)
            time.sleep(TEST_DURACAO_SEGUNDOS)
            controller.parar()
        else:
            mensagens.append('Eixo TILT: movimento não confirmado visualmente.')

        del frame_c, frame_d
    except Exception as e:
        logger.error(f"[PTZ {cam_id}] Erro ao testar TILT: {e}")
        mensagens.append(f'Erro ao testar TILT: {e}')

    resultado['sucesso'] = resultado['pan_detectado'] or resultado['tilt_detectado']
    resultado['mensagem'] = ' '.join(mensagens) if mensagens else \
        'Calibração concluída com sucesso! Eixos detectados automaticamente.'

    gc.collect()
    return resultado


# ═══════════════════════════════════════════════════════════════════════════
#  PRESETS - CRUD
# ═══════════════════════════════════════════════════════════════════════════
def _listar_presets(db_path: str, cam_id: int) -> List[dict]:
    with get_conexao(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM ptz_presets WHERE camera_id=? ORDER BY ordem ASC",
            (cam_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def _inserir_preset(db_path: str, cam_id: int, nome: str,
                    token: str, tempo_parada: int = 8,
                    tipo: str = 'camera',
                    pan: Optional[float] = None,
                    tilt: Optional[float] = None,
                    zoom: Optional[float] = None) -> int:
    with get_conexao(db_path) as conn:
        maxord = conn.execute(
            "SELECT COALESCE(MAX(ordem), -1) as m FROM ptz_presets "
            "WHERE camera_id=?", (cam_id,)
        ).fetchone()['m']
        cur = conn.execute(
            """INSERT INTO ptz_presets
               (camera_id, nome, token, ordem, tempo_parada,
                tipo, pan, tilt, zoom)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cam_id, nome, token, maxord + 1, tempo_parada,
             tipo, pan, tilt, zoom)
        )
        return cur.lastrowid


def _remover_preset(db_path: str, preset_id: int):
    with get_conexao(db_path) as conn:
        conn.execute("DELETE FROM ptz_presets WHERE id=?", (preset_id,))


def _atualizar_ordem_presets(db_path: str, ordem_ids: List[int]):
    with get_conexao(db_path) as conn:
        for idx, pid in enumerate(ordem_ids):
            conn.execute(
                "UPDATE ptz_presets SET ordem=? WHERE id=?", (idx, pid)
            )


def _atualizar_tempo_preset(db_path: str, preset_id: int, tempo: int):
    with get_conexao(db_path) as conn:
        conn.execute(
            "UPDATE ptz_presets SET tempo_parada=? WHERE id=?",
            (tempo, preset_id)
        )


def _ir_para_preset(controller: 'PTZController', preset: dict,
                    velocidade: float):
    tipo = preset.get('tipo') or 'camera'
    if tipo == 'local' and preset.get('pan') is not None:
        controller.mover_absoluto(
            preset['pan'], preset['tilt'], preset.get('zoom') or 0.0,
            velocidade
        )
    elif tipo == 'virtual' and preset.get('pan') is not None:
        controller.mover_para_virtual(
            preset['pan'], preset['tilt'], velocidade
        )
    else:
        controller.goto_preset(preset['token'], velocidade)


# ═══════════════════════════════════════════════════════════════════════════
#  PATRULHA AUTOMÁTICA
# ═══════════════════════════════════════════════════════════════════════════
class PatrulhaPTZ:
    def __init__(self, cam_id: int, db_path: str):
        self.cam_id  = cam_id
        self.db_path = db_path
        self.ativo   = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=f'patrulha-{cam_id}'
        )
        self._thread.start()

    def _aguardar_interrompivel(self, segundos: float):
        decorrido = 0.0
        while decorrido < segundos and self.ativo:
            time.sleep(0.5)
            decorrido += 0.5

    def _loop(self):
        from modulos.streaming import notificar_ptz_estado

        logger.info(f"[PTZ {self.cam_id}] ► Patrulha iniciada.")

        while self.ativo:
            try:
                presets = _listar_presets(self.db_path, self.cam_id)
                config  = _carregar_config_camera(self.db_path, self.cam_id)

                if not presets or not config:
                    time.sleep(5)
                    continue

                controller = obter_controller(self.db_path, self.cam_id)
                if controller is None:
                    time.sleep(5)
                    continue

                for preset in presets:
                    if not self.ativo:
                        break

                    while self.ativo and tracking_ocupado(self.cam_id):
                        time.sleep(0.5)
                    if not self.ativo:
                        break

                    config_atual = _carregar_config_camera(
                        self.db_path, self.cam_id
                    ) or config
                    velocidade = config_atual.get('ptz_velocidade_patrulha', 0.5)
                    continua   = bool(config_atual.get('ptz_patrulha_continua'))

                    notificar_ptz_estado(self.cam_id, True)
                    _patrulha_transito[self.cam_id] = True
                    movimento_ok = True
                    try:
                        _ir_para_preset(controller, preset, velocidade)
                        logger.debug(
                            f"[PTZ {self.cam_id}] Patrulha → "
                            f"'{preset['nome']}' "
                            f"(tipo={preset.get('tipo','camera')}"
                            f"{', contínua' if continua else ''})"
                        )
                    except Exception as e:
                        movimento_ok = False
                        logger.error(
                            f"[PTZ {self.cam_id}] Erro ao ir para "
                            f"preset '{preset['nome']}': {e}"
                        )

                    if movimento_ok:
                        buffer_mov = (TEMPO_BUFFER_MOVIMENTO_CONTINUO
                                     if continua else TEMPO_BUFFER_MOVIMENTO)
                        self._aguardar_interrompivel(buffer_mov)
                    notificar_ptz_estado(self.cam_id, False)
                    _patrulha_transito[self.cam_id] = False

                    if not movimento_ok:
                        self._aguardar_interrompivel(COOLDOWN_RECONEXAO)
                        continue

                    if continua:
                        self._aguardar_interrompivel(TEMPO_ESTABILIZAR_CONTINUO)
                    else:
                        tempo_parada = preset.get('tempo_parada', 8)
                        self._aguardar_interrompivel(tempo_parada)

            except Exception as e:
                logger.error(
                    f"[PTZ {self.cam_id}] Erro no loop de patrulha: {e}",
                    exc_info=True
                )
                time.sleep(5)

        _patrulha_transito.pop(self.cam_id, None)
        logger.info(f"[PTZ {self.cam_id}] ■ Patrulha encerrada.")

    def parar(self):
        self.ativo = False
        if self._thread.is_alive():
            self._thread.join(timeout=5)


_patrulhas      : Dict[int, PatrulhaPTZ] = {}
_patrulhas_lock : threading.Lock         = threading.Lock()


def iniciar_patrulha(db_path: str, cam_id: int):
    with _patrulhas_lock:
        if cam_id in _patrulhas:
            return
        _patrulhas[cam_id] = PatrulhaPTZ(cam_id, db_path)


def parar_patrulha(cam_id: int):
    with _patrulhas_lock:
        p = _patrulhas.pop(cam_id, None)
    if p:
        p.parar()


def patrulha_ativa(cam_id: int) -> bool:
    with _patrulhas_lock:
        return cam_id in _patrulhas


def encerrar_todas_patrulhas():
    with _patrulhas_lock:
        itens = list(_patrulhas.items())
        _patrulhas.clear()
    for cam_id, p in itens:
        p.parar()
    encerrar_todos_trackings()
    logger.info("Todas as patrulhas PTZ encerradas.")


def iniciar_patrulhas_salvas(db_path: str):
    _garantir_colunas_tracking(db_path)
    time.sleep(3)
    try:
        with get_conexao(db_path) as conn:
            rows = conn.execute(
                "SELECT id FROM cameras WHERE ptz_ativo=1 "
                "AND ptz_patrulha_ativa=1 AND ativa=1"
            ).fetchall()
        for row in rows:
            iniciar_patrulha(db_path, row['id'])
            time.sleep(0.5)
        logger.info(f"{len(rows)} patrulha(s) PTZ iniciada(s).")
    except Exception as e:
        logger.error(f"Erro ao iniciar patrulhas salvas: {e}")

    try:
        with get_conexao(db_path) as conn:
            rows = conn.execute(
                "SELECT id FROM cameras WHERE ptz_ativo=1 "
                "AND ptz_tracking=1 AND ativa=1"
            ).fetchall()
        for row in rows:
            iniciar_tracking(db_path, row['id'])
            time.sleep(0.5)
        logger.info(f"{len(rows)} tracking(s) PTZ iniciado(s).")
    except Exception as e:
        logger.error(f"Erro ao iniciar trackings salvos: {e}")


# ═══════════════════════════════════════════════════════════════════════════
#  AUTO-TRACKING
# ═══════════════════════════════════════════════════════════════════════════
TRACK_INTERVALO_OCIOSO = 1.0
TRACK_INTERVALO_ATIVO  = 0.25
TRACK_PERDA_ALVO_S     = 1.5
TRACK_RETORNO_ESTABILIZAR_S = 4.0
TRACK_PATRULHA_PAUSA_S = 10
TRACK_MANUAL_PAUSA_S   = 4

_track_ultimo_alvo : Dict[int, float] = {}
_track_retornando  : Dict[int, bool]  = {}   # câmera voltando à posição inicial
_patrulha_transito : Dict[int, bool]  = {}   # patrulha deslocando a câmera
TRACK_RETORNO_MAX_S = 15.0
_manual_ate        : Dict[int, float] = {}
_track_status      : Dict[int, dict]  = {}
_trackers          : Dict[int, 'TrackerPTZ'] = {}
_trackers_lock     : threading.Lock = threading.Lock()


def marcar_controle_manual(cam_id: int, segundos: float = TRACK_MANUAL_PAUSA_S):
    _manual_ate[cam_id] = time.time() + segundos


def manual_ativo(cam_id: int) -> bool:
    return time.time() < _manual_ate.get(cam_id, 0.0)


def tracking_ocupado(cam_id: int) -> bool:
    if _track_retornando.get(cam_id):
        return True
    return (time.time() - _track_ultimo_alvo.get(cam_id, 0.0)
            < TRACK_PATRULHA_PAUSA_S)


def _garantir_colunas_tracking(db_path: str):
    """Mantido por segurança/retrocompatibilidade."""
    try:
        with get_conexao(db_path) as conn:
            cols = {r['name'] for r in conn.execute("PRAGMA table_info(cameras)")}
            if 'ptz_tracking' not in cols:
                conn.execute("ALTER TABLE cameras ADD COLUMN ptz_tracking "
                             "INTEGER NOT NULL DEFAULT 0")
            if 'ptz_tracking_zona' not in cols:
                conn.execute("ALTER TABLE cameras ADD COLUMN ptz_tracking_zona "
                             "INTEGER NOT NULL DEFAULT 20")
            if 'ptz_patrulha_continua' not in cols:
                conn.execute("ALTER TABLE cameras ADD COLUMN ptz_patrulha_continua "
                             "INTEGER NOT NULL DEFAULT 0")

            cols_presets = {r['name'] for r in
                           conn.execute("PRAGMA table_info(ptz_presets)")}
            if 'tipo' not in cols_presets:
                conn.execute("ALTER TABLE ptz_presets ADD COLUMN tipo "
                             "TEXT NOT NULL DEFAULT 'camera'")
            if 'pan' not in cols_presets:
                conn.execute("ALTER TABLE ptz_presets ADD COLUMN pan REAL DEFAULT NULL")
            if 'tilt' not in cols_presets:
                conn.execute("ALTER TABLE ptz_presets ADD COLUMN tilt REAL DEFAULT NULL")
            if 'zoom' not in cols_presets:
                conn.execute("ALTER TABLE ptz_presets ADD COLUMN zoom REAL DEFAULT NULL")
    except Exception as e:
        logger.error(f"Erro ao criar colunas de tracking/presets: {e}")


def _patrulha_disponivel(db_path: str, cam_id: int, cfg: Optional[dict]) -> bool:
    if not cfg or not cfg.get('ptz_patrulha_ativa'):
        return False
    try:
        return len(_listar_presets(db_path, cam_id)) > 0
    except Exception:
        return False


def _categorias_tracking(cfg: dict):
    modo = cfg.get('modo_gravacao') or 'movimento'
    if modo in ('pessoa', 'veiculo', 'animal'):
        cats = {modo}
    elif modo == 'pessoa_especifica':
        cats = {'pessoa'}
    else:
        cats = set()
        if cfg.get('detectar_pessoa'):  cats.add('pessoa')
        if cfg.get('detectar_veiculo'): cats.add('veiculo')
        if cfg.get('detectar_animal'):  cats.add('animal')
    nomes = {'pessoa': 'pessoas', 'veiculo': 'veículos', 'animal': 'animais'}
    texto = ', '.join(nomes[c] for c in ('pessoa', 'veiculo', 'animal')
                      if c in cats) or 'nada (nenhuma categoria ativa na IA)'
    if modo == 'pessoa_especifica':
        texto = 'pessoa específica cadastrada'
    return cats, texto


def _centro(bbox):
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def _area(bbox):
    return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])


class TrackerPTZ:
    def __init__(self, cam_id: int, db_path: str):
        self.cam_id  = cam_id
        self.db_path = db_path
        self.ativo   = True
        self._movendo = False
        self._ult_centro = None
        self._ult_reconhecido = 0.0
        self._ult_alvo        = None
        self._ult_alvo_ts     = 0.0
        self._home_usa_token  = False

        self._estado             = 'idle'
        self._home_pos           : Optional[dict]  = None
        self._home_preset_token  : Optional[str]   = None
        self._home_virtual       : Optional[Tuple[float, float]] = None
        self._retorno_ts         = 0.0

        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=f'tracker-{cam_id}'
        )
        self._thread.start()

    def _status(self, msg: str):
        _track_status[self.cam_id] = {'msg': msg, 'ts': time.time()}

    def _parar_camera(self, controller):
        if self._movendo:
            try:
                controller.parar()
            except Exception as e:
                logger.warning(f"[Tracking {self.cam_id}] Falha ao parar: {e}")
            self._movendo = False

    def _dormir(self, segundos: float):
        t = 0.0
        while t < segundos and self.ativo:
            time.sleep(0.1)
            t += 0.1

    def _capturar_home(self, controller: 'PTZController'):
        if controller._suporta_status is not False:
            status = controller.obter_status()
            if status:
                self._home_pos      = status
                self._home_virtual  = None
                self._home_usa_token = False
                logger.debug(
                    f"[Tracking {self.cam_id}] 📍 Posição inicial memorizada "
                    f"(GetStatus): pan={status['pan']:.3f} "
                    f"tilt={status['tilt']:.3f}"
                )
                return

        if controller._suporta_preset is not False:
            try:
                token = controller.set_preset(
                    'RRFS_TRACK_HOME', token=self._home_preset_token
                )
                if token:
                    self._home_preset_token = token
                    self._home_pos          = None
                    self._home_virtual      = None
                    self._home_usa_token    = True
                    logger.debug(
                        f"[Tracking {self.cam_id}] 📍 Posição inicial "
                        f"memorizada via preset temporário (token={token})."
                    )
                    return
            except Exception as e:
                logger.debug(
                    f"[Tracking {self.cam_id}] Preset temporário "
                    f"indisponível: {e}"
                )

        pan_v, tilt_v = controller.obter_posicao_virtual()
        self._home_virtual = (pan_v, tilt_v)
        self._home_pos      = None
        self._home_usa_token = False
        logger.debug(
            f"[Tracking {self.cam_id}] 📍 Posição inicial memorizada via "
            f"controle por tempo (virtual): pan={pan_v:.2f} tilt={tilt_v:.2f}"
        )

    def _iniciar_retorno_home(self, controller: 'PTZController', cfg: dict):
        velocidade = float(cfg.get('ptz_velocidade_manual') or 0.5)

        if self._home_pos:
            ok = controller.mover_absoluto(
                self._home_pos['pan'], self._home_pos['tilt'],
                self._home_pos.get('zoom', 0.0), velocidade
            )
            if ok:
                self._status('↩ Alvo saiu de cena — retornando à posição inicial')
                self._retorno_ts = time.time()
                return

        if self._home_preset_token and self._home_usa_token:
            try:
                controller.goto_preset(self._home_preset_token, velocidade)
                self._status('↩ Alvo saiu de cena — retornando à posição inicial')
                self._retorno_ts = time.time()
                return
            except Exception as e:
                logger.warning(
                    f"[Tracking {self.cam_id}] Falha ao retornar via "
                    f"preset: {e}"
                )

        if self._home_virtual:
            try:
                controller.mover_para_virtual(
                    self._home_virtual[0], self._home_virtual[1], velocidade
                )
                self._status('↩ Alvo saiu de cena — retornando à posição inicial')
                self._retorno_ts = time.time()
                return
            except Exception as e:
                logger.warning(
                    f"[Tracking {self.cam_id}] Falha ao retornar via "
                    f"posição virtual: {e}"
                )

        self._status(
            'Alvo saiu de cena — posição inicial desconhecida'
        )
        self._retorno_ts = time.time()

    def _retorno_concluido(self, controller: 'PTZController') -> bool:
        decorrido = time.time() - self._retorno_ts
        if decorrido < 1.0:
            return False
        if self._home_pos and controller._suporta_status is not False:
            try:
                st = controller.obter_status()
                if st:
                    return (abs(st['pan']  - self._home_pos['pan'])  < 0.04 and
                            abs(st['tilt'] - self._home_pos['tilt']) < 0.04)
            except Exception:
                pass
        return decorrido > TRACK_RETORNO_ESTABILIZAR_S

    def _escolher_alvo(self, res: dict, cfg: dict):
        objs = res.get('objetos', [])
        modo = cfg.get('modo_gravacao')

        if modo == 'pessoa_especifica' and cfg.get('pessoa_alvo_id'):
            alvo_id = cfg['pessoa_alvo_id']
            agora = time.time()
            for p in res.get('pessoas_identificadas', []):
                if p.get('pessoa_id') == alvo_id:
                    self._ult_centro = _centro(p['bbox'])
                    self._ult_reconhecido = agora
                    return p['bbox']
            if self._ult_centro and agora - self._ult_reconhecido < 5.0:
                pessoas = [o for o in objs if o['tipo'] == 'pessoa']
                if pessoas:
                    ux, uy = self._ult_centro
                    melhor = min(pessoas, key=lambda o: (
                        (_centro(o['bbox'])[0] - ux) ** 2 +
                        (_centro(o['bbox'])[1] - uy) ** 2))
                    self._ult_centro = _centro(melhor['bbox'])
                    return melhor['bbox']
            return None

        if not objs:
            return None

        agora = time.time()
        escolhido = None
        if self._ult_alvo and agora - self._ult_alvo_ts < 2.0:
            ux, uy = self._ult_alvo
            perto = min(objs, key=lambda o: (
                (_centro(o['bbox'])[0] - ux) ** 2 +
                (_centro(o['bbox'])[1] - uy) ** 2))
            cx, cy = _centro(perto['bbox'])
            altura = max(1, perto['bbox'][3] - perto['bbox'][1])
            if ((cx - ux) ** 2 + (cy - uy) ** 2) ** 0.5 < 2.5 * altura:
                escolhido = perto
        if escolhido is None:
            escolhido = max(objs, key=lambda o: _area(o['bbox']))
        self._ult_alvo    = _centro(escolhido['bbox'])
        self._ult_alvo_ts = agora
        return escolhido['bbox']

    @staticmethod
    def _nivel(e: float) -> float:
        """
        v9 - CORREÇÃO: curva de aceleração muito mais agressiva.

        Antes (v8), a velocidade só chegava perto do máximo quando o
        alvo estava quase saindo da borda da imagem (erro ≈ 0.5), o que
        fazia a câmera reagir tarde demais para alvos que atravessam
        rápido o campo de visão — o alvo já tinha ido embora antes da
        câmera acelerar de verdade.

        Agora a velocidade sobe em curva de raiz quadrada (sqrt),
        atingindo valores próximos do máximo já com um deslocamento
        pequeno do centro, e o piso mínimo também subiu (de 0.35 para
        0.5) para vencer a inércia de motores mais lentos desde o
        primeiro comando enviado.
        """
        a         = min(abs(e), 0.5)
        proporcao = (a / 0.5) ** 0.5     # sqrt → sobe rápido no início
        minimo    = 0.5                  # piso mais alto (era 0.35)
        return max(minimo, min(1.0, minimo + proporcao * (1.0 - minimo)))

    def _seguir(self, controller, cfg: dict, bbox, w: int, h: int):
        cx, cy = _centro(bbox)
        ex = cx / w - 0.5
        ey = cy / h - 0.5
        morta = (float(cfg.get('ptz_tracking_zona') or 20) / 100.0) / 2.0

        pan = tilt = 0.0
        if abs(ex) > morta:
            pan = (1.0 if ex > 0 else -1.0) * self._nivel(ex)
        if abs(ey) > morta:
            tilt = (-1.0 if ey > 0 else 1.0) * self._nivel(ey)

        if cfg.get('ptz_invert_pan'):
            pan = -pan
        if cfg.get('ptz_invert_tilt'):
            tilt = -tilt

        if pan == 0.0 and tilt == 0.0:
            self._parar_camera(controller)
            self._status('Alvo centralizado')
        else:
            controller.mover_continuo(pan, tilt, 0.0)
            self._movendo = True
            self._status('Seguindo alvo')

    def _loop(self):
        from modulos.streaming import obter_frame_cv2, obter_frame_limpo
        from modulos import ia_deteccao

        logger.info(f"[Tracking {self.cam_id}] ► Iniciado.")
        ultimo_visto = 0.0

        while self.ativo:
            intervalo = TRACK_INTERVALO_OCIOSO
            controller = None
            try:
                cfg = _carregar_config_camera(self.db_path, self.cam_id)
                if not cfg or not cfg.get('ptz_tracking'):
                    break
                if not cfg.get('ptz_ativo'):
                    self._status('PTZ desativado nesta câmera')
                    self._dormir(2)
                    continue

                controller = obter_controller(self.db_path, self.cam_id)
                if controller is None:
                    self._status('PTZ indisponível')
                    self._dormir(3)
                    continue

                if manual_ativo(self.cam_id):
                    self._movendo = False
                    self._estado = 'idle'
                    self._status('Controle manual em uso')
                    self._dormir(0.5)
                    continue

                if not cfg.get('ia_ativa'):
                    self._parar_camera(controller)
                    self._estado = 'idle'
                    self._status('IA desativada — ative a IA desta câmera')
                    self._dormir(2)
                    continue

                if _patrulha_transito.get(self.cam_id) and \
                   self._estado != 'seguindo':
                    self._status('Patrulha em deslocamento')
                    self._dormir(0.3)
                    continue

                cats, _ = _categorias_tracking(cfg)
                if not cats:
                    self._status('Nenhuma categoria ativa na IA')
                    self._dormir(2)
                    continue

                frame = obter_frame_limpo(self.cam_id)
                if frame is None:
                    frame = obter_frame_cv2(self.cam_id)
                if frame is None:
                    self._status('Aguardando imagem da câmera')
                    self._dormir(1)
                    continue
                h, w = frame.shape[:2]

                ia_cfg = {
                    'detectar_pessoa'      : 'pessoa'  in cats,
                    'detectar_veiculo'     : 'veiculo' in cats,
                    'detectar_animal'      : 'animal'  in cats,
                    'reconhecimento_facial': (cfg.get('modo_gravacao') ==
                                              'pessoa_especifica' and
                                              bool(cfg.get('reconhecimento_facial'))),
                    'confianca_minima'     : float(cfg.get('confianca_minima') or 0.5),
                }
                res  = ia_deteccao.processar_frame_completo(frame, ia_cfg)
                bbox = self._escolher_alvo(res, cfg)
                del frame

                agora = time.time()

                if bbox is None:
                    if self._estado == 'seguindo' and \
                       agora - ultimo_visto > TRACK_PERDA_ALVO_S:
                        self._parar_camera(controller)

                        self._estado = 'retornando'
                        _track_retornando[self.cam_id] = True
                        self._iniciar_retorno_home(controller, cfg)

                    elif self._estado == 'retornando':
                        if self._retorno_concluido(controller) or \
                           agora - self._retorno_ts > TRACK_RETORNO_MAX_S:
                            self._estado = 'idle'
                            _track_retornando[self.cam_id] = False
                            self._status('Sem alvo — na posição inicial')

                    else:
                        self._status('Sem alvo — aguardando')

                else:
                    if self._estado != 'seguindo':
                        tem_home = bool(self._home_pos or self._home_virtual or
                                        (self._home_preset_token and
                                         self._home_usa_token))
                        # Durante o retorno, a posição inicial JÁ está
                        # memorizada: não sobrescreve com ponto intermediário.
                        if self._estado == 'idle' or not tem_home:
                            self._capturar_home(controller)
                        _track_retornando[self.cam_id] = False
                        self._estado = 'seguindo'

                    ultimo_visto = agora
                    _track_ultimo_alvo[self.cam_id] = agora
                    intervalo = TRACK_INTERVALO_ATIVO
                    self._seguir(controller, cfg, bbox, w, h)

            except Exception as e:
                logger.error(f"[Tracking {self.cam_id}] Erro: {e}", exc_info=True)
                self._status(f'Erro: {e}')
                intervalo = 3.0

            self._dormir(intervalo)

        try:
            ctrl = obter_controller(self.db_path, self.cam_id)
            if ctrl is not None:
                cfg_final = _carregar_config_camera(
                    self.db_path, self.cam_id
                ) or {}
                if self._estado == 'seguindo':
                    self._parar_camera(ctrl)
                    self._iniciar_retorno_home(ctrl, cfg_final)
                else:
                    self._parar_camera(ctrl)

                if self._home_preset_token:
                    try:
                        ctrl.remove_preset(self._home_preset_token)
                    except Exception:
                        pass
        except Exception:
            pass

        _track_status.pop(self.cam_id, None)
        _track_retornando.pop(self.cam_id, None)
        logger.info(f"[Tracking {self.cam_id}] ■ Encerrado.")

    def parar(self):
        self.ativo = False
        if self._thread.is_alive() and \
           threading.current_thread() is not self._thread:
            self._thread.join(timeout=6)


def iniciar_tracking(db_path: str, cam_id: int):
    with _trackers_lock:
        t = _trackers.get(cam_id)
        if t and t._thread.is_alive():
            return
        _trackers[cam_id] = TrackerPTZ(cam_id, db_path)


def parar_tracking(cam_id: int):
    with _trackers_lock:
        t = _trackers.pop(cam_id, None)
    if t:
        t.parar()


def tracking_ativo(cam_id: int) -> bool:
    with _trackers_lock:
        t = _trackers.get(cam_id)
        return bool(t and t._thread.is_alive())


def encerrar_todos_trackings():
    with _trackers_lock:
        itens = list(_trackers.values())
        _trackers.clear()
    for t in itens:
        t.parar()


# ═══════════════════════════════════════════════════════════════════════════
#  MAPA DE DIREÇÕES
# ═══════════════════════════════════════════════════════════════════════════
MAPA_DIRECAO = {
    'cima'    : (0.0,  1.0, 0.0),
    'baixo'   : (0.0, -1.0, 0.0),
    'esquerda': (-1.0, 0.0, 0.0),
    'direita' : (1.0,  0.0, 0.0),
    'zoom_in' : (0.0,  0.0, 1.0),
    'zoom_out': (0.0,  0.0,-1.0),
}


# ═══════════════════════════════════════════════════════════════════════════
#  ROTAS - Página de Controle
# ═══════════════════════════════════════════════════════════════════════════
@ptz_bp.route('/camera/ptz/<int:cam_id>')
@login_required
def editor_ptz(cam_id: int):
    db = current_app.config['DB_PATH']
    try:
        with get_conexao(db) as conn:
            cam = conn.execute(
                "SELECT * FROM cameras WHERE id=?", (cam_id,)
            ).fetchone()
            if not cam:
                flash('Câmera não encontrada.', 'danger')
                return redirect(url_for('cameras.configuracoes'))
            cam = dict(cam)
    except Exception as e:
        flash(f'Erro: {e}', 'danger')
        return redirect(url_for('cameras.configuracoes'))

    _garantir_colunas_tracking(db)
    presets = _listar_presets(db, cam_id)
    cam = _carregar_config_camera(db, cam_id) or cam
    tracking_alvos = _categorias_tracking(cam)[1]

    return render_template(
        'ptz_controle.html',
        cam=cam,
        tracking_alvos=tracking_alvos,
        presets=presets,
        onvif_disponivel=ptz_disponivel(),
        patrulha_rodando=patrulha_ativa(cam_id),
        usuario=current_user.nome
    )


@ptz_bp.route('/camera/ptz/<int:cam_id>/config', methods=['POST'])
@login_required
def salvar_config_ptz(cam_id: int):
    db = current_app.config['DB_PATH']

    ptz_ativo      = request.form.get('ptz_ativo') == '1'
    onvif_porta    = request.form.get('onvif_porta', '80').strip()
    onvif_usuario  = request.form.get('onvif_usuario', '').strip()
    onvif_senha    = request.form.get('onvif_senha', '').strip()
    onvif_tipo     = request.form.get('onvif_tipo', 'netcat_onvif').strip()
    onvif_caminho  = request.form.get('onvif_caminho', '').strip() \
                     or '/onvif/deviceio_service'
    onvif_parada   = request.form.get('onvif_parada', 'auto').strip()

    if onvif_tipo not in ('netcat_onvif', 'auto'):
        onvif_tipo = 'netcat_onvif'
    if onvif_parada not in ('auto', 'stop', 'zero'):
        onvif_parada = 'auto'
    if not onvif_caminho.startswith('/'):
        onvif_caminho = '/' + onvif_caminho

    try:
        porta_int = int(onvif_porta)
    except Exception:
        porta_int = 80

    try:
        with get_conexao(db) as conn:
            conn.execute(
                """UPDATE cameras SET ptz_ativo=?, onvif_porta=?,
                   onvif_usuario=?, onvif_senha=?, onvif_tipo=?,
                   onvif_caminho=?, onvif_parada=? WHERE id=?""",
                (int(ptz_ativo), porta_int, onvif_usuario,
                 onvif_senha, onvif_tipo, onvif_caminho,
                 onvif_parada, cam_id)
            )
        invalidar_controller(cam_id)
        flash('Configuração ONVIF salva com sucesso!', 'success')
    except Exception as e:
        logger.error(f"Erro ao salvar config PTZ: {e}")
        flash(f'Erro: {e}', 'danger')

    return redirect(url_for('ptz.editor_ptz', cam_id=cam_id))


# ═══════════════════════════════════════════════════════════════════════════
#  ROTAS - Controle Manual
# ═══════════════════════════════════════════════════════════════════════════
@ptz_bp.route('/api/ptz/<int:cam_id>/mover', methods=['POST'])
@login_required
def api_mover(cam_id: int):
    from modulos.streaming import notificar_ptz_estado

    t_req = time.time()
    db   = current_app.config['DB_PATH']
    data = request.get_json(silent=True) or {}
    direcao = data.get('direcao', '')

    if direcao not in MAPA_DIRECAO:
        return jsonify({'ok': False, 'erro': 'Direção inválida'}), 400

    config = _carregar_config_camera(db, cam_id)
    if not config or not config.get('ptz_ativo'):
        return jsonify({'ok': False, 'erro': 'PTZ não ativado nesta câmera'}), 400

    velocidade = float(data.get('velocidade',
                       config.get('ptz_velocidade_manual', 0.5)))
    velocidade = max(0.1, min(1.0, velocidade))

    pan_base, tilt_base, zoom_base = MAPA_DIRECAO[direcao]
    pan  = pan_base  * velocidade
    tilt = tilt_base * velocidade
    zoom = zoom_base * velocidade

    if config.get('ptz_invert_pan'):
        pan = -pan
    if config.get('ptz_invert_tilt'):
        tilt = -tilt

    controller = obter_controller(db, cam_id)
    if controller is None:
        return jsonify({'ok': False, 'erro': 'Não foi possível conectar via ONVIF'}), 500

    try:
        marcar_controle_manual(cam_id)
        controller.mover_continuo(pan, tilt, zoom, t_req=t_req)
        notificar_ptz_estado(cam_id, True)
        return jsonify({'ok': True})
    except Exception as e:
        logger.error(f"[PTZ {cam_id}] Erro ao mover: {e}")
        notificar_ptz_estado(cam_id, False)
        return jsonify({'ok': False, 'erro': str(e)}), 500


@ptz_bp.route('/api/ptz/<int:cam_id>/parar', methods=['POST'])
@login_required
def api_parar(cam_id: int):
    from modulos.streaming import notificar_ptz_estado

    db = current_app.config['DB_PATH']
    controller = obter_controller(db, cam_id)
    if controller is None:
        return jsonify({'ok': False, 'erro': 'PTZ não configurado'}), 400

    try:
        parou = controller.parar()
        if parou:
            notificar_ptz_estado(cam_id, False)
        return jsonify({'ok': True})
    except Exception as e:
        logger.warning(f"[PTZ {cam_id}] Não foi possível parar: {e}")
        notificar_ptz_estado(cam_id, False)
        return jsonify({'ok': False, 'erro': str(e)}), 200


# ═══════════════════════════════════════════════════════════════════════════
#  ROTAS - Auto-Calibração
# ═══════════════════════════════════════════════════════════════════════════
@ptz_bp.route('/api/ptz/<int:cam_id>/calibrar', methods=['POST'])
@login_required
def api_calibrar(cam_id: int):
    from modulos.streaming import obter_frame_cv2

    db     = current_app.config['DB_PATH']
    config = _carregar_config_camera(db, cam_id)

    if not config or not config.get('ptz_ativo'):
        return jsonify({
            'ok': False,
            'erro': 'Ative o PTZ e configure o ONVIF primeiro'
        }), 400

    controller = obter_controller(db, cam_id)
    if controller is None:
        return jsonify({'ok': False, 'erro': 'Falha ao preparar controlador PTZ'}), 500

    marcar_controle_manual(cam_id, 90)
    resultado = executar_calibracao(cam_id, controller, obter_frame_cv2)

    if resultado.get('pan_detectado') or resultado.get('tilt_detectado'):
        try:
            with get_conexao(db) as conn:
                conn.execute(
                    """UPDATE cameras SET ptz_calibrado=1,
                       ptz_invert_pan=?, ptz_invert_tilt=? WHERE id=?""",
                    (int(resultado.get('invert_pan', False)),
                     int(resultado.get('invert_tilt', False)), cam_id)
                )
        except Exception as e:
            logger.error(f"Erro ao salvar calibração: {e}")

    return jsonify({'ok': resultado.get('sucesso', False), **resultado})


@ptz_bp.route('/api/ptz/<int:cam_id>/velocidade', methods=['POST'])
@login_required
def api_salvar_velocidade(cam_id: int):
    db   = current_app.config['DB_PATH']
    data = request.get_json(silent=True) or {}
    try:
        v = max(0.1, min(1.0, float(data.get('velocidade', 0.5))))
        with get_conexao(db) as conn:
            conn.execute(
                "UPDATE cameras SET ptz_velocidade_manual=? WHERE id=?",
                (v, cam_id)
            )
        return jsonify({'ok': True, 'velocidade': v})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)}), 500


@ptz_bp.route('/api/ptz/<int:cam_id>/inverter', methods=['POST'])
@login_required
def api_inverter(cam_id: int):
    db   = current_app.config['DB_PATH']
    data = request.get_json(silent=True) or {}
    invert_pan  = bool(data.get('invert_pan', False))
    invert_tilt = bool(data.get('invert_tilt', False))

    try:
        with get_conexao(db) as conn:
            conn.execute(
                """UPDATE cameras SET ptz_invert_pan=?, ptz_invert_tilt=?,
                   ptz_calibrado=1 WHERE id=?""",
                (int(invert_pan), int(invert_tilt), cam_id)
            )
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════
#  ROTAS - Presets
# ═══════════════════════════════════════════════════════════════════════════
@ptz_bp.route('/api/ptz/<int:cam_id>/presets', methods=['GET'])
@login_required
def api_listar_presets(cam_id: int):
    db = current_app.config['DB_PATH']
    return jsonify(_listar_presets(db, cam_id))


@ptz_bp.route('/api/ptz/<int:cam_id>/presets/adicionar', methods=['POST'])
@login_required
def api_adicionar_preset(cam_id: int):
    db   = current_app.config['DB_PATH']
    data = request.get_json(silent=True) or {}
    nome = (data.get('nome') or '').strip()
    tempo_parada = int(data.get('tempo_parada', 8))

    if not nome:
        return jsonify({'ok': False, 'erro': 'Nome do preset é obrigatório'}), 400

    controller = obter_controller(db, cam_id)
    if controller is None:
        return jsonify({'ok': False, 'erro': 'PTZ não configurado/conectado'}), 400

    if controller._suporta_preset is not False:
        try:
            token = controller.set_preset(nome)
            if token:
                preset_id = _inserir_preset(db, cam_id, nome, token,
                                            tempo_parada, tipo='camera')
                logger.info(
                    f"[PTZ {cam_id}] Preset '{nome}' salvo (nativo da câmera)."
                )
                return jsonify({
                    'ok': True, 'id': preset_id, 'token': token, 'tipo': 'camera'
                })
        except Exception as e:
            logger.warning(
                f"[PTZ {cam_id}] Câmera não suporta SetPreset nativo ONVIF "
                f"({e}) — tentando próximo método."
            )

    if controller._suporta_status is not False:
        status = controller.obter_status()
        if status:
            token_local = f"local:{uuid.uuid4().hex}"
            preset_id = _inserir_preset(
                db, cam_id, nome, token_local, tempo_parada, tipo='local',
                pan=status['pan'], tilt=status['tilt'],
                zoom=status.get('zoom', 0.0)
            )
            logger.info(
                f"[PTZ {cam_id}] Preset '{nome}' salvo via posição absoluta "
                f"REAL (câmera sem suporte a SetPreset nativo)."
            )
            return jsonify({
                'ok': True, 'id': preset_id, 'token': token_local, 'tipo': 'local'
            })

    pan_v, tilt_v = controller.obter_posicao_virtual()
    token_virtual = f"virtual:{uuid.uuid4().hex}"
    preset_id = _inserir_preset(
        db, cam_id, nome, token_virtual, tempo_parada, tipo='virtual',
        pan=pan_v, tilt=tilt_v, zoom=0.0
    )
    logger.info(
        f"[PTZ {cam_id}] Preset '{nome}' salvo via posição VIRTUAL "
        f"(câmera sem SetPreset nem GetStatus — controle 100% por tempo, "
        f"conduzido pelo sistema)."
    )
    return jsonify({
        'ok': True, 'id': preset_id, 'token': token_virtual, 'tipo': 'virtual',
        'aviso': ('Esta câmera não possui presets nem leitura de posição '
                  'próprios. A posição foi salva usando controle por tempo '
                  '(o sistema conduz o movimento). Funciona bem no uso '
                  'normal, mas reinicie e re-salve as posições caso o '
                  'servidor seja reiniciado e note desalinhamento.')
    })


@ptz_bp.route('/api/ptz/<int:cam_id>/presets/<int:preset_id>/remover',
             methods=['POST'])
@login_required
def api_remover_preset(cam_id: int, preset_id: int):
    db = current_app.config['DB_PATH']
    try:
        with get_conexao(db) as conn:
            row = conn.execute(
                "SELECT token, tipo FROM ptz_presets WHERE id=?", (preset_id,)
            ).fetchone()

        if row and row['tipo'] == 'camera':
            controller = obter_controller(db, cam_id)
            if controller:
                try:
                    controller.remove_preset(row['token'])
                except Exception as e:
                    logger.warning(
                        f"[PTZ {cam_id}] Não foi possível remover preset "
                        f"na câmera: {e}"
                    )

        _remover_preset(db, preset_id)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)}), 500


@ptz_bp.route('/api/ptz/<int:cam_id>/presets/<int:preset_id>/ir',
             methods=['POST'])
@login_required
def api_ir_preset(cam_id: int, preset_id: int):
    from modulos.streaming import notificar_ptz_estado

    db = current_app.config['DB_PATH']
    try:
        with get_conexao(db) as conn:
            row = conn.execute(
                "SELECT * FROM ptz_presets WHERE id=?", (preset_id,)
            ).fetchone()

        if not row:
            return jsonify({'ok': False, 'erro': 'Preset não encontrado'}), 404
        row = dict(row)

        controller = obter_controller(db, cam_id)
        if controller is None:
            return jsonify({'ok': False, 'erro': 'PTZ não conectado'}), 400

        config = _carregar_config_camera(db, cam_id)
        velocidade = config.get('ptz_velocidade_manual', 0.5) if config else 0.5

        tempo_estimado = 3.0
        if row.get('tipo') == 'virtual' and row.get('pan') is not None:
            tempo_estimado = controller.estimar_tempo_virtual(
                row['pan'], row['tilt'], velocidade
            ) + 1.0

        marcar_controle_manual(cam_id, max(10, tempo_estimado + 3))
        notificar_ptz_estado(cam_id, True)

        def _executar():
            try:
                _ir_para_preset(controller, row, velocidade)
            except Exception as e:
                logger.error(
                    f"[PTZ {cam_id}] Erro ao ir para preset '{row.get('nome')}': {e}"
                )
            finally:
                notificar_ptz_estado(cam_id, False)

        threading.Thread(target=_executar, daemon=True,
                         name=f'ptz-ir-{cam_id}').start()

        return jsonify({'ok': True, 'tipo': row.get('tipo', 'camera')})
    except Exception as e:
        return jsonify({'ok': False, 'erro': str(e)}), 500


@ptz_bp.route('/api/ptz/<int:cam_id>/presets/<int:preset_id>/tempo',
             methods=['POST'])
@login_required
def api_atualizar_tempo_preset(cam_id: int, preset_id: int):
    db   = current_app.config['DB_PATH']
    data = request.get_json(silent=True) or {}
    tempo = int(data.get('tempo_parada', 8))
    tempo = max(2, min(300, tempo))
    _atualizar_tempo_preset(db, preset_id, tempo)
    return jsonify({'ok': True})


@ptz_bp.route('/api/ptz/<int:cam_id>/presets/reordenar', methods=['POST'])
@login_required
def api_reordenar_presets(cam_id: int):
    db   = current_app.config['DB_PATH']
    data = request.get_json(silent=True) or {}
    ordem = data.get('ordem', [])

    if not isinstance(ordem, list):
        return jsonify({'ok': False, 'erro': 'Formato inválido'}), 400

    _atualizar_ordem_presets(db, ordem)
    return jsonify({'ok': True})


# ═══════════════════════════════════════════════════════════════════════════
#  ROTAS - Modo de Operação
# ═══════════════════════════════════════════════════════════════════════════
@ptz_bp.route('/api/ptz/<int:cam_id>/modo', methods=['POST'])
@login_required
def api_definir_modo(cam_id: int):
    db   = current_app.config['DB_PATH']
    data = request.get_json(silent=True) or {}
    modo = data.get('modo', 'manual')
    velocidade_patrulha = float(data.get('velocidade_patrulha', 0.5))
    velocidade_patrulha = max(0.1, min(1.0, velocidade_patrulha))
    continua = bool(data.get('continua', False))

    if modo not in ('manual', 'patrulha'):
        modo = 'manual'

    try:
        if modo == 'patrulha':
            presets = _listar_presets(db, cam_id)
            if not presets:
                return jsonify({
                    'ok': False,
                    'erro': 'Cadastre ao menos 1 preset antes de ativar a patrulha'
                }), 400

            with get_conexao(db) as conn:
                conn.execute(
                    """UPDATE cameras SET modo_ptz='patrulha',
                       ptz_patrulha_ativa=1, ptz_velocidade_patrulha=?,
                       ptz_patrulha_continua=? WHERE id=?""",
                    (velocidade_patrulha, int(continua), cam_id)
                )
            iniciar_patrulha(db, cam_id)
        else:
            with get_conexao(db) as conn:
                conn.execute(
                    "UPDATE cameras SET modo_ptz='manual', "
                    "ptz_patrulha_ativa=0 WHERE id=?", (cam_id,)
                )
            parar_patrulha(cam_id)

        return jsonify({'ok': True, 'modo': modo, 'continua': continua})
    except Exception as e:
        logger.error(f"[PTZ {cam_id}] Erro ao definir modo: {e}")
        return jsonify({'ok': False, 'erro': str(e)}), 500


@ptz_bp.route('/api/ptz/<int:cam_id>/patrulha_continua', methods=['POST'])
@login_required
def api_patrulha_continua(cam_id: int):
    db   = current_app.config['DB_PATH']
    data = request.get_json(silent=True) or {}
    continua = bool(data.get('continua', False))
    try:
        with get_conexao(db) as conn:
            conn.execute(
                "UPDATE cameras SET ptz_patrulha_continua=? WHERE id=?",
                (int(continua), cam_id)
            )
        return jsonify({'ok': True, 'continua': continua})
    except Exception as e:
        logger.error(f"[PTZ {cam_id}] Erro ao salvar patrulha contínua: {e}")
        return jsonify({'ok': False, 'erro': str(e)}), 500


@ptz_bp.route('/api/ptz/<int:cam_id>/testar', methods=['POST'])
@login_required
def api_testar_conexao(cam_id: int):
    """Testa a conexão ONVIF com as configurações SALVAS."""
    db = current_app.config['DB_PATH']
    invalidar_controller(cam_id)
    controller = obter_controller(db, cam_id)
    if controller is None:
        return jsonify({'ok': False,
                        'erro': 'Ative o PTZ e salve a configuração primeiro'})
    ok = controller.conectar()
    return jsonify({
        'ok'      : ok,
        'caminho' : controller._path,
        'perfil'  : controller._profile_token,
        'auth'    : MODOS_AUTH[controller._auth_idx],
        'erro'    : controller.erro_atual(),
        'pan_max' : controller._pan_max,
        'tilt_max': controller._tilt_max,
        'zoom_max': controller._zoom_max,
        'escala_padrao_onvif': (controller._pan_max <= 1.01 and
                                controller._tilt_max <= 1.01),
    })


@ptz_bp.route('/api/ptz/<int:cam_id>/tracking', methods=['POST'])
@login_required
def api_tracking(cam_id: int):
    db   = current_app.config['DB_PATH']
    data = request.get_json(silent=True) or {}
    ativo = bool(data.get('ativo', False))
    try:
        zona = max(5, min(50, int(data.get('zona', 20))))
    except Exception:
        zona = 20

    _garantir_colunas_tracking(db)
    cfg = _carregar_config_camera(db, cam_id)
    if not cfg:
        return jsonify({'ok': False, 'erro': 'Câmera não encontrada'}), 404

    if ativo:
        if not cfg.get('ptz_ativo'):
            return jsonify({'ok': False,
                            'erro': 'Ative o PTZ e salve a conexão primeiro'}), 400
        if not cfg.get('ia_ativa'):
            return jsonify({'ok': False,
                            'erro': 'Ative a IA desta câmera primeiro (o tracking '
                                    'segue o que está configurado na IA)'}), 400
        if not _categorias_tracking(cfg)[0]:
            return jsonify({'ok': False,
                            'erro': 'Nenhuma categoria ativa na IA '
                                    '(pessoa/veículo/animal)'}), 400

    try:
        with get_conexao(db) as conn:
            conn.execute(
                "UPDATE cameras SET ptz_tracking=?, ptz_tracking_zona=? "
                "WHERE id=?", (int(ativo), zona, cam_id)
            )
        if ativo:
            iniciar_tracking(db, cam_id)
        else:
            parar_tracking(cam_id)
        return jsonify({'ok': True, 'ativo': ativo, 'zona': zona})
    except Exception as e:
        logger.error(f"[PTZ {cam_id}] Erro ao salvar tracking: {e}")
        return jsonify({'ok': False, 'erro': str(e)}), 500


@ptz_bp.route('/api/ptz/<int:cam_id>/medir_velocidade', methods=['POST'])
@login_required
def api_medir_velocidade(cam_id: int):
    """Gira a câmera a 30/60/100% e mede o deslocamento real da imagem."""
    from modulos.streaming import obter_frame_cv2

    db = current_app.config['DB_PATH']
    controller = obter_controller(db, cam_id)
    if controller is None:
        return jsonify({'ok': False,
                        'erro': 'Ative o PTZ e salve a conexão primeiro'}), 400

    marcar_controle_manual(cam_id, 60)
    DUR = 0.8
    resultados = []
    try:
        for v in (0.3, 0.6, 1.0):
            fa = obter_frame_cv2(cam_id)
            if fa is None:
                return jsonify({'ok': False,
                                'erro': 'Sem imagem da câmera'}), 500

            controller.mover_continuo(v, 0.0)
            t0 = time.time()
            time.sleep(DUR)
            t1 = time.time()
            controller.parar()
            time.sleep(0.7)

            fb = obter_frame_cv2(cam_id)
            dx, dy, resp = _detectar_deslocamento(fa, fb) \
                if fb is not None else (0.0, 0.0, 0.0)
            resultados.append({
                'velocidade': v,
                'px_por_s'  : round(abs(dx) / max(0.1, t1 - t0), 1),
                'confianca' : round(resp, 2),
            })

            controller.mover_continuo(-v, 0.0)
            time.sleep(DUR)
            controller.parar()
            time.sleep(0.8)
            del fa, fb
    except Exception as e:
        logger.error(f"[PTZ {cam_id}] Erro ao medir velocidade: {e}")
        return jsonify({'ok': False, 'erro': str(e)}), 500
    finally:
        marcar_controle_manual(cam_id, 4)

    p_lenta  = resultados[0]['px_por_s']
    p_maxima = resultados[2]['px_por_s']
    if p_maxima < 3 and p_lenta < 3:
        conclusao = ('Não consegui medir (pouco detalhe na imagem ou a '
                     'câmera quase não se moveu).')
    elif p_maxima / max(p_lenta, 0.1) < 1.35:
        conclusao = ('A velocidade enviada praticamente NÃO muda o resultado: '
                     'a câmera gira numa velocidade fixa via ONVIF (limite do '
                     'firmware/motor), OU a escala detectada automaticamente '
                     f'(pan=±{controller._pan_max:g}) já estava compensando. '
                     'O tracking funciona, mas segue devagar.')
    else:
        conclusao = ('A câmera respeita o parâmetro de velocidade. '
                     f'Na máxima, atravessar a imagem leva ~'
                     f'{round(640 / max(p_maxima, 1), 1)} s. '
                     f'(escala aplicada: pan=±{controller._pan_max:g})')

    return jsonify({'ok': True, 'resultados': resultados,
                    'conclusao': conclusao,
                    'pan_max': controller._pan_max,
                    'tilt_max': controller._tilt_max})


@ptz_bp.route('/api/ptz/<int:cam_id>/status')
@login_required
def api_status_ptz(cam_id: int):
    db = current_app.config['DB_PATH']
    erro = ''
    escala_info = None
    with _controllers_lock:
        controller = _controllers.get(cam_id)
    if controller:
        erro = controller.erro_atual()
        if controller._escala_detectada:
            escala_info = {
                'pan_max' : controller._pan_max,
                'tilt_max': controller._tilt_max,
                'zoom_max': controller._zoom_max,
            }

    cfg = _carregar_config_camera(db, cam_id) or {}
    st  = _track_status.get(cam_id, {})

    return jsonify({
        'patrulha_ativa'    : patrulha_ativa(cam_id),
        'patrulha_continua' : bool(cfg.get('ptz_patrulha_continua')),
        'erro_conexao'      : erro,
        'tracking_ativo'    : tracking_ativo(cam_id),
        'tracking_msg'      : st.get('msg', ''),
        'tracking_alvos'    : _categorias_tracking(cfg)[1] if cfg else '',
        'escala_velocidade' : escala_info,
    })
