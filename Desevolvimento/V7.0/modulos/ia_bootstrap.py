#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Bootstrap de Modelos de IA Facial
Garante que os arquivos de modelo do dlib/face_recognition existam
localmente, baixando-os automaticamente na primeira execução.

Elimina a dependência do pacote 'face_recognition_models' via pip/git
(que costuma vir incompleto na PyPI e trava em conexões lentas via git clone).

Os modelos são baixados diretamente do repositório oficial do dlib
(davisking/dlib-models) e armazenados em dados/modelos_ia/.
"""

import os
import sys
import bz2
import logging
import urllib.request
import urllib.error

logger = logging.getLogger('RRFS.ia_bootstrap')

BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELOS_DIR = os.path.join(BASE_DIR, 'dados', 'modelos_ia')

TIMEOUT_DOWNLOAD = 120  # segundos
TENTATIVAS       = 3

# ── Fonte oficial dos modelos (mesma usada pelo pacote face_recognition_models) ──
_MODELOS = {
    'pose_predictor_68': {
        'arquivo': 'shape_predictor_68_face_landmarks.dat',
        'url': 'https://github.com/davisking/dlib-models/raw/master/'
               'shape_predictor_68_face_landmarks.dat.bz2',
    },
    'pose_predictor_5': {
        'arquivo': 'shape_predictor_5_face_landmarks.dat',
        'url': 'https://github.com/davisking/dlib-models/raw/master/'
               'shape_predictor_5_face_landmarks.dat.bz2',
    },
    'face_recognition_model': {
        'arquivo': 'dlib_face_recognition_resnet_model_v1.dat',
        'url': 'https://github.com/davisking/dlib-models/raw/master/'
               'dlib_face_recognition_resnet_model_v1.dat.bz2',
    },
    'cnn_face_detector': {
        'arquivo': 'mmod_human_face_detector.dat',
        'url': 'https://github.com/davisking/dlib-models/raw/master/'
               'mmod_human_face_detector.dat.bz2',
    },
}

# Tamanho mínimo aceitável para considerar o arquivo válido (evita
# considerar um download corrompido/parcial como "pronto")
_TAMANHO_MINIMO_BYTES = 100 * 1024  # 100 KB


def _baixar_e_descompactar(url: str, destino: str) -> bool:
    """Baixa um .bz2 com retry e descompacta para o destino final."""
    tmp_bz2 = destino + '.bz2.tmp'

    for tentativa in range(1, TENTATIVAS + 1):
        try:
            logger.info(
                f"⬇️  Baixando modelo facial: {os.path.basename(destino)} "
                f"(tentativa {tentativa}/{TENTATIVAS})..."
            )
            req = urllib.request.Request(
                url, headers={'User-Agent': 'RRFS-Cameras/1.0'}
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT_DOWNLOAD) as resp, \
                 open(tmp_bz2, 'wb') as f_out:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    f_out.write(chunk)

            logger.info(f"📦 Descompactando {os.path.basename(destino)}...")
            with bz2.BZ2File(tmp_bz2, 'rb') as f_in, \
                 open(destino, 'wb') as f_out:
                while True:
                    chunk = f_in.read(1024 * 1024)
                    if not chunk:
                        break
                    f_out.write(chunk)

            os.remove(tmp_bz2)

            if os.path.getsize(destino) < _TAMANHO_MINIMO_BYTES:
                raise IOError("Arquivo descompactado menor que o esperado.")

            logger.info(f"✓ Modelo pronto: {os.path.basename(destino)}")
            return True

        except Exception as e:
            logger.warning(
                f"Falha ao baixar/descompactar {os.path.basename(destino)} "
                f"(tentativa {tentativa}): {e}"
            )
            for f in (tmp_bz2, destino):
                if os.path.exists(f):
                    try:
                        os.remove(f)
                    except Exception:
                        pass

    logger.error(
        f"✕ Não foi possível obter o modelo: {os.path.basename(destino)} "
        f"após {TENTATIVAS} tentativas."
    )
    return False


def garantir_modelos_faciais() -> bool:
    """
    Garante que todos os arquivos de modelo existam em disco,
    baixando automaticamente os que estiverem faltando ou corrompidos.

    Retorna True se TODOS os modelos estiverem disponíveis e prontos
    (e aplica o monkeypatch necessário para o face_recognition usá-los).
    """
    os.makedirs(MODELOS_DIR, exist_ok=True)

    caminhos  = {}
    tudo_ok   = True

    for chave, info in _MODELOS.items():
        destino = os.path.join(MODELOS_DIR, info['arquivo'])
        caminhos[chave] = destino

        if os.path.exists(destino) and \
           os.path.getsize(destino) >= _TAMANHO_MINIMO_BYTES:
            continue  # já existe e parece válido

        ok = _baixar_e_descompactar(info['url'], destino)
        if not ok:
            tudo_ok = False

    if tudo_ok:
        _aplicar_monkeypatch(caminhos)
        logger.info("✓ Todos os modelos faciais estão prontos localmente.")
    else:
        logger.warning(
            "⚠️ Alguns modelos faciais não puderam ser baixados. "
            "Reconhecimento facial pode ficar indisponível."
        )

    return tudo_ok


def _aplicar_monkeypatch(caminhos: dict):
    """
    Garante que exista um módulo 'face_recognition_models' em
    sys.modules apontando para os arquivos baixados localmente —
    seja substituindo as funções do pacote real (se instalado),
    seja criando um módulo virtual (se o pacote não existir).

    Isso precisa ser chamado ANTES de 'import face_recognition'.
    """
    try:
        import face_recognition_models as frm
        frm.pose_predictor_model_location = \
            lambda: caminhos['pose_predictor_68']
        frm.pose_predictor_five_point_model_location = \
            lambda: caminhos['pose_predictor_5']
        frm.face_recognition_model_location = \
            lambda: caminhos['face_recognition_model']
        frm.cnn_face_detector_model_location = \
            lambda: caminhos['cnn_face_detector']
        logger.info(
            "✓ Pacote face_recognition_models existente redirecionado "
            "para modelos locais (dados/modelos_ia/)."
        )

    except Exception:
        # Pacote não instalado (ou quebrado) → cria módulo virtual em memória
        import types

        modulo_fake = types.ModuleType('face_recognition_models')
        modulo_fake.pose_predictor_model_location = \
            lambda: caminhos['pose_predictor_68']
        modulo_fake.pose_predictor_five_point_model_location = \
            lambda: caminhos['pose_predictor_5']
        modulo_fake.face_recognition_model_location = \
            lambda: caminhos['face_recognition_model']
        modulo_fake.cnn_face_detector_model_location = \
            lambda: caminhos['cnn_face_detector']

        sys.modules['face_recognition_models'] = modulo_fake
        logger.info(
            "✓ Módulo 'face_recognition_models' virtual criado em memória "
            "(pacote pip não instalado/quebrado) — usando modelos locais."
        )
