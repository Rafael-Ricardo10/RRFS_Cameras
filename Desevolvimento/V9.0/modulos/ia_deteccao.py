#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Módulo de Inteligência Artificial
Detecção de Objetos (YOLOv8) + Reconhecimento Facial
Auto-detecção de Hardware (CPU/GPU) · Singleton Thread-Safe · Zero Memory Leak
"""

import os
import sys
import gc
import cv2
import json
import pickle
import logging
import threading
import time
import uuid
import numpy as np
from typing      import Dict, List, Optional, Tuple
from datetime     import datetime

from flask         import (Blueprint, render_template, request,
                           redirect, url_for, flash, current_app,
                           jsonify, send_file, abort)
from flask_login   import login_required
from werkzeug.utils import secure_filename

from modulos.database import get_conexao

logger = logging.getLogger('RRFS.ia')
ia_bp  = Blueprint('ia', __name__)


# ── Importações seguras (não quebra o sistema se não instalado) ───────────
try:
    import torch
    _TORCH_OK = True
except ImportError:
    _TORCH_OK = False
    logger.warning(
        "⚠️ PyTorch não instalado. Detecção de objetos ficará desabilitada. "
        "Instale com: pip install torch"
    )

try:
    from ultralytics import YOLO
    _YOLO_OK = True
except ImportError:
    _YOLO_OK = False
    logger.warning(
        "⚠️ Ultralytics não instalado. Detecção de objetos ficará desabilitada. "
        "Instale com: pip install ultralytics"
    )

# ── Bootstrap: garante os modelos faciais ANTES de importar face_recognition ──
try:
    from modulos.ia_bootstrap import garantir_modelos_faciais
    _MODELOS_FACIAIS_OK = garantir_modelos_faciais()
except Exception as e:
    _MODELOS_FACIAIS_OK = False
    logger.error(f"Erro no bootstrap de modelos faciais: {e}")

try:
    if not _MODELOS_FACIAIS_OK:
        raise ImportError("Modelos faciais locais indisponíveis.")
    import face_recognition
    _FACE_OK = True
except Exception as e:
    _FACE_OK = False
    logger.warning(
        f"⚠️ Reconhecimento facial indisponível ({e}). "
        "Instale com: pip install face_recognition"
    )


# ── Classes de interesse (COCO dataset) ────────────────────────────────────
CLASSES_INTERESSE = {
    0 : 'pessoa',
    1 : 'bicicleta',
    2 : 'carro',
    3 : 'moto',
    5 : 'onibus',
    7 : 'caminhao',
    15: 'gato',
    16: 'cachorro',
}

CATEGORIA_MAP = {
    'pessoa'    : 'pessoa',
    'bicicleta' : 'veiculo',
    'carro'     : 'veiculo',
    'moto'      : 'veiculo',
    'onibus'    : 'veiculo',
    'caminhao'  : 'veiculo',
    'gato'      : 'animal',
    'cachorro'  : 'animal',
}

# ── Tolerância facial (quanto menor, mais rígido) ──────────────────────────
TOLERANCIA_FACIAL_PADRAO = 0.6

# ── Cooldown para não floodar galeria de rostos (segundos) ────────────────
COOLDOWN_SALVAR_ROSTO = 20

# ── Resolução do caminho do modelo YOLO (persistente, fora do binário) ───
# Mesma lógica usada em servidor.py / ia_bootstrap.py: em produção
# (binário compilado via Nuitka), o modelo fica em RRFS_DADOS_DIR/
# modelos_ia/yolov8n.pt — pasta que sobrevive a atualizações do
# executável. Se o arquivo não existir ali (nem foi pré-empacotado pelo
# instalador), o ultralytics tenta baixá-lo automaticamente nesse mesmo
# caminho (precisa de internet, só na 1ª execução).
def _resolver_dados_dir_ia() -> str:
    if '__compiled__' in globals():
        base = os.path.dirname(os.path.abspath(sys.argv[0]))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.environ.get('RRFS_DADOS_DIR', os.path.join(base, 'dados'))


_YOLO_MODELO_PADRAO = os.path.join(
    _resolver_dados_dir_ia(), 'modelos_ia', 'yolov8n.pt'
)

# ═══════════════════════════════════════════════════════════════════════════
#  MOTOR DE IA (Singleton)
# ═══════════════════════════════════════════════════════════════════════════
class MotorIA:
    """
    Motor de Inteligência Artificial - Singleton Global.

    - Carrega o modelo YOLO UMA ÚNICA VEZ na memória (compartilhado
      entre todas as câmeras via lock de inferência).
    - Auto-detecta hardware: usa GPU (CUDA) se disponível, senão CPU.
    - Mantém cache de encodings faciais das pessoas cadastradas em RAM.
    - Todas as operações liberam memória explicitamente (sem vazamento).
    """

    _instancia      = None
    _lock_singleton = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock_singleton:
            if cls._instancia is None:
                cls._instancia = super().__new__(cls)
                cls._instancia._inicializado = False
            return cls._instancia

    def __init__(self, modelo_path: Optional[str] = None):
        if self._inicializado:
            return
        self._inicializado = True

        # Caminho padrão agora aponta para dados/modelos_ia/yolov8n.pt
        # (persistente, fora do binário) em vez de um caminho relativo.
        modelo_path = modelo_path or _YOLO_MODELO_PADRAO
        os.makedirs(os.path.dirname(modelo_path), exist_ok=True)

        self.disponivel = False
        self.device     = 'cpu'
        self.modelo     = None
        self.modelo_path = modelo_path

    # ── Hardware ─────────────────────────────────────────────────────────
    def _detectar_hardware(self):
        """Detecta automaticamente se há GPU NVIDIA disponível."""
        if _TORCH_OK and torch.cuda.is_available():
            self.device = 'cuda'
            try:
                gpu_nome = torch.cuda.get_device_name(0)
                logger.info(f"🎮 GPU detectada: {gpu_nome} · usando CUDA")
            except Exception:
                logger.info("🎮 GPU CUDA detectada.")
        else:
            self.device = 'cpu'
            logger.info(
                "🖥️ Nenhuma GPU detectada · usando CPU para inferência de IA"
            )

    def recarregar_hardware(self):
        """
        Redetecta hardware em tempo real (ex: GPU foi instalada depois).
        Move o modelo já carregado para o novo device, sem reiniciar o servidor.
        """
        device_anterior = self.device
        self._detectar_hardware()

        if self.device != device_anterior and self.modelo is not None:
            try:
                self.modelo.to(self.device)
                logger.info(
                    f"✓ IA migrada: {device_anterior.upper()} → "
                    f"{self.device.upper()}"
                )
            except Exception as e:
                logger.error(f"Erro ao migrar modelo para {self.device}: {e}")
                self.device = device_anterior

    def info_hardware(self) -> dict:
        """Retorna informações de hardware para exibir na interface."""
        info = {
            'device'         : self.device,
            'gpu_disponivel' : False,
            'gpu_nome'       : None,
            'yolo_ok'        : _YOLO_OK and self.disponivel,
            'face_ok'        : _FACE_OK,
            'torch_ok'       : _TORCH_OK,
        }
        if _TORCH_OK and torch.cuda.is_available():
            info['gpu_disponivel'] = True
            try:
                info['gpu_nome'] = torch.cuda.get_device_name(0)
                info['gpu_memoria_total_mb'] = round(
                    torch.cuda.get_device_properties(0).total_memory
                    / 1024 / 1024
                )
            except Exception:
                pass
        return info

    # ── Modelo YOLO ──────────────────────────────────────────────────────
    def _carregar_modelo(self, modelo_path: str):
        if not _YOLO_OK:
            logger.error(
                "Ultralytics ausente → detecção de objetos DESATIVADA."
            )
            return
        try:
            logger.info(f"Carregando modelo YOLO: {modelo_path}...")
            self.modelo = YOLO(modelo_path)
            self.modelo.to(self.device)
            self.disponivel = True
            logger.info(
                f"✓ Modelo YOLO carregado com sucesso · device={self.device}"
            )
        except Exception as e:
            logger.error(f"Erro ao carregar modelo YOLO: {e}", exc_info=True)
            self.disponivel = False

    # ── Detecção de Objetos ──────────────────────────────────────────────
    def detectar_objetos(self, frame: np.ndarray,
                         confianca_min: float = 0.5,
                         categorias_permitidas: Optional[set] = None
                         ) -> List[Dict]:
        """
        Detecta objetos no frame usando YOLOv8.
        categorias_permitidas: {'pessoa', 'veiculo', 'animal'} ou None (todas)
        Retorna lista de dicts: {tipo, categoria, confianca, bbox}
        """
        if not self.disponivel or self.modelo is None:
            return []

        objetos = []
        try:
            with self._lock_inferencia:
                resultados = self.modelo.predict(
                    frame,
                    verbose=False,
                    conf=confianca_min,
                    device=self.device,
                    imgsz=416  # menor = mais rápido, ideal p/ CPU
                )

            r = resultados[0]
            for box in r.boxes:
                classe_id = int(box.cls[0])
                if classe_id not in CLASSES_INTERESSE:
                    continue

                nome_classe = CLASSES_INTERESSE[classe_id]
                categoria   = CATEGORIA_MAP.get(nome_classe, 'outro')

                if categorias_permitidas and \
                   categoria not in categorias_permitidas:
                    continue

                confianca      = float(box.conf[0])
                x1, y1, x2, y2 = box.xyxy[0].tolist()

                objetos.append({
                    'tipo'      : nome_classe,
                    'categoria' : categoria,
                    'confianca' : round(confianca, 2),
                    'bbox'      : (int(x1), int(y1), int(x2), int(y2))
                })

            # Libera referências explicitamente (evita acúmulo em GPU/CPU)
            del resultados, r

            self._contador_inferencias += 1
            if self._contador_inferencias % 200 == 0:
                gc.collect()
                if self.device == 'cuda' and _TORCH_OK:
                    torch.cuda.empty_cache()

        except Exception as e:
            logger.error(f"Erro na detecção YOLO: {e}", exc_info=True)

        return objetos

    # ── Reconhecimento Facial ────────────────────────────────────────────
    def recarregar_pessoas(self, db_path: str):
        """
        Recarrega os encodings faciais do banco para a memória.
        Chamado sempre que uma pessoa é cadastrada/removida.
        """
        if not _FACE_OK:
            return
        try:
            with get_conexao(db_path) as conn:
                rows = conn.execute(
                    "SELECT id, nome, face_encoding "
                    "FROM pessoas_cadastradas WHERE ativo = 1"
                ).fetchall()

            nomes, ids, encodings = [], [], []
            for row in rows:
                try:
                    enc = pickle.loads(row['face_encoding'])
                    nomes.append(row['nome'])
                    ids.append(row['id'])
                    encodings.append(enc)
                except Exception as e:
                    logger.error(
                        f"Erro ao carregar encoding de '{row['nome']}': {e}"
                    )

            with self._pessoas_lock:
                self._pessoas_nomes     = nomes
                self._pessoas_ids       = ids
                self._pessoas_encodings = encodings

            logger.info(
                f"🧠 {len(nomes)} pessoa(s) carregada(s) para "
                f"reconhecimento facial."
            )
        except Exception as e:
            logger.error(f"Erro ao recarregar pessoas: {e}")

    def reconhecer_rosto(self, frame: np.ndarray,
                         bbox: Tuple[int, int, int, int],
                         tolerancia: float = TOLERANCIA_FACIAL_PADRAO
                         ) -> Optional[Dict]:
        """
        Recorta a região da pessoa detectada, localiza o rosto
        e compara com os rostos cadastrados.

        Retorna: {nome, pessoa_id, confianca, foto_rosto} ou None
        (None = nenhum rosto encontrado na região)
        """
        if not _FACE_OK:
            return None

        x1, y1, x2, y2 = bbox
        h, w = frame.shape[:2]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)

        if x2 <= x1 or y2 <= y1:
            return None

        regiao = frame[y1:y2, x1:x2]
        if regiao.size == 0:
            return None

        rgb = None
        try:
            rgb = cv2.cvtColor(regiao, cv2.COLOR_BGR2RGB)
            localizacoes = face_recognition.face_locations(rgb, model='hog')

            if not localizacoes:
                return None

            encodings = face_recognition.face_encodings(rgb, localizacoes)
            if not encodings:
                return None

            encoding_detectado = encodings[0]

            with self._pessoas_lock:
                nomes      = list(self._pessoas_nomes)
                conhecidos = list(self._pessoas_encodings)
                ids        = list(self._pessoas_ids)

            resultado = {
                'nome'       : 'Desconhecido',
                'pessoa_id'  : None,
                'confianca'  : 0.0,
                'foto_rosto' : regiao.copy()
            }

            if conhecidos:
                distancias  = face_recognition.face_distance(
                    conhecidos, encoding_detectado
                )
                idx_melhor  = int(np.argmin(distancias))
                menor_dist  = distancias[idx_melhor]

                if menor_dist <= tolerancia:
                    confianca = round((1 - menor_dist) * 100, 1)
                    resultado['nome']      = nomes[idx_melhor]
                    resultado['pessoa_id'] = ids[idx_melhor]
                    resultado['confianca'] = confianca

                del distancias

            del encodings, localizacoes, encoding_detectado
            return resultado

        except Exception as e:
            logger.error(f"Erro no reconhecimento facial: {e}")
            return None
        finally:
            if rgb is not None:
                del rgb

    @staticmethod
    def gerar_encoding_de_arquivo(filepath: str) -> Optional[np.ndarray]:
        """
        Usado ao CADASTRAR uma nova pessoa.
        Lê uma foto e extrai o encoding facial (vetor de 128 dimensões).
        """
        if not _FACE_OK:
            return None
        try:
            img  = face_recognition.load_image_file(filepath)
            encs = face_recognition.face_encodings(img)
            del img
            if not encs:
                return None
            return encs[0]
        except Exception as e:
            logger.error(f"Erro ao gerar encoding facial: {e}")
            return None


# ── Instância global (lazy init - só carrega quando necessário) ───────────
_motor_ia   : Optional[MotorIA] = None
_motor_lock : threading.Lock    = threading.Lock()


def obter_motor_ia() -> MotorIA:
    """Retorna a instância singleton do motor de IA (cria se necessário)."""
    global _motor_ia
    with _motor_lock:
        if _motor_ia is None:
            _motor_ia = MotorIA()
        return _motor_ia


def ia_objetos_disponivel() -> bool:
    return _YOLO_OK and _TORCH_OK


def ia_facial_disponivel() -> bool:
    return _FACE_OK


# ═══════════════════════════════════════════════════════════════════════════
#  PIPELINE DE PROCESSAMENTO (usado pelo streaming.py)
# ═══════════════════════════════════════════════════════════════════════════
def processar_frame_completo(frame: np.ndarray, ia_config: dict) -> dict:
    """
    Pipeline completo: detecta objetos configurados + reconhece rostos.

    ia_config = {
        'detectar_pessoa'       : bool,
        'detectar_veiculo'      : bool,
        'detectar_animal'       : bool,
        'reconhecimento_facial' : bool,
        'confianca_minima'      : float,
    }

    Retorna:
    {
        'objetos'               : [...],
        'pessoas_identificadas' : [{nome, pessoa_id, confianca, bbox, foto_rosto}],
        'tem_pessoa'            : bool,
        'tem_veiculo'           : bool,
        'tem_animal'            : bool,
    }
    """
    resultado = {
        'objetos'               : [],
        'pessoas_identificadas' : [],
        'tem_pessoa'            : False,
        'tem_veiculo'           : False,
        'tem_animal'            : False,
    }

    motor = obter_motor_ia()
    if not motor.disponivel:
        return resultado

    categorias = set()
    if ia_config.get('detectar_pessoa'):   categorias.add('pessoa')
    if ia_config.get('detectar_veiculo'):  categorias.add('veiculo')
    if ia_config.get('detectar_animal'):   categorias.add('animal')

    if not categorias:
        return resultado

    confianca_min = float(ia_config.get('confianca_minima', 0.5))
    objetos = motor.detectar_objetos(frame, confianca_min, categorias)
    resultado['objetos'] = objetos

    for obj in objetos:
        if obj['categoria'] == 'pessoa':
            resultado['tem_pessoa'] = True
        elif obj['categoria'] == 'veiculo':
            resultado['tem_veiculo'] = True
        elif obj['categoria'] == 'animal':
            resultado['tem_animal'] = True

    if ia_config.get('reconhecimento_facial') and resultado['tem_pessoa']:
        for obj in objetos:
            if obj['tipo'] != 'pessoa':
                continue
            info_rosto = motor.reconhecer_rosto(frame, obj['bbox'])
            if info_rosto:
                info_rosto['bbox'] = obj['bbox']
                resultado['pessoas_identificadas'].append(info_rosto)

    return resultado


# ═══════════════════════════════════════════════════════════════════════════
#  ROTAS - Configuração de Hardware
# ═══════════════════════════════════════════════════════════════════════════
@ia_bp.route('/api/ia/hardware')
@login_required
def api_hardware():
    """Retorna informações de hardware (CPU/GPU) em uso pela IA."""
    motor = obter_motor_ia()
    info  = motor.info_hardware()

    db = current_app.config['DB_PATH']
    try:
        with get_conexao(db) as conn:
            total_pessoas = conn.execute(
                "SELECT COUNT(*) as t FROM pessoas_cadastradas WHERE ativo=1"
            ).fetchone()['t']
    except Exception:
        total_pessoas = 0

    info['total_pessoas_cadastradas'] = total_pessoas
    return jsonify(info)


@ia_bp.route('/api/ia/redetectar_hardware', methods=['POST'])
@login_required
def api_redetectar_hardware():
    """
    Força nova detecção de hardware (ex: usuário acabou de instalar GPU).
    Não precisa reiniciar o servidor.
    """
    motor = obter_motor_ia()
    motor.recarregar_hardware()
    return jsonify({
        'ok'   : True,
        'info' : motor.info_hardware()
    })


# ═══════════════════════════════════════════════════════════════════════════
#  ROTAS - Pessoas Cadastradas (Reconhecimento Facial)
# ═══════════════════════════════════════════════════════════════════════════
@ia_bp.route('/pessoas')
@login_required
def listar_pessoas():
    """Página de gerenciamento de pessoas cadastradas."""
    db = current_app.config['DB_PATH']
    pessoas = []
    try:
        with get_conexao(db) as conn:
            rows = conn.execute(
                "SELECT * FROM pessoas_cadastradas "
                "WHERE ativo = 1 ORDER BY nome"
            ).fetchall()
            pessoas = [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Erro ao listar pessoas: {e}")

    return render_template(
        'ia_pessoas.html',
        pessoas=pessoas,
        facial_disponivel=ia_facial_disponivel()
    )


@ia_bp.route('/pessoas/adicionar', methods=['POST'])
@login_required
def adicionar_pessoa():
    """Cadastra uma nova pessoa a partir de uma foto com rosto visível."""
    if not ia_facial_disponivel():
        flash(
            'Reconhecimento facial não disponível. '
            'Instale: pip install face_recognition', 'danger'
        )
        return redirect(url_for('ia.listar_pessoas'))

    nome = request.form.get('nome', '').strip()
    foto = request.files.get('foto')

    if not nome or len(nome) < 2:
        flash('Nome é obrigatório (mín. 2 caracteres).', 'danger')
        return redirect(url_for('ia.listar_pessoas'))

    if not foto or foto.filename == '':
        flash('Selecione uma foto com o rosto da pessoa.', 'danger')
        return redirect(url_for('ia.listar_pessoas'))

    ext = foto.filename.rsplit('.', 1)[-1].lower() if '.' in foto.filename else ''
    if ext not in ('jpg', 'jpeg', 'png'):
        flash('Formato inválido. Use JPG ou PNG.', 'danger')
        return redirect(url_for('ia.listar_pessoas'))

    pessoas_path = current_app.config['PESSOAS_PATH']
    filename     = f"{uuid.uuid4().hex}.{ext}"
    filepath     = os.path.join(pessoas_path, secure_filename(filename))

    try:
        foto.save(filepath)

        # Gera o encoding facial da foto
        encoding = MotorIA.gerar_encoding_de_arquivo(filepath)
        if encoding is None:
            os.remove(filepath)
            flash(
                'Nenhum rosto detectado na foto. '
                'Use uma foto nítida, de frente.', 'danger'
            )
            return redirect(url_for('ia.listar_pessoas'))

        encoding_blob = pickle.dumps(encoding)

        db = current_app.config['DB_PATH']
        with get_conexao(db) as conn:
            conn.execute(
                """INSERT INTO pessoas_cadastradas
                   (nome, foto_path, face_encoding, ativo)
                   VALUES (?, ?, ?, 1)""",
                (nome, filename, encoding_blob)
            )

        # Recarrega cache de encodings em memória
        motor = obter_motor_ia()
        motor.recarregar_pessoas(db)

        logger.info(f"✓ Pessoa cadastrada: '{nome}'")
        flash(f'Pessoa "{nome}" cadastrada com sucesso!', 'success')

    except Exception as e:
        if os.path.exists(filepath):
            os.remove(filepath)
        logger.error(f"Erro ao cadastrar pessoa: {e}")
        flash(f'Erro ao cadastrar: {e}', 'danger')

    return redirect(url_for('ia.listar_pessoas'))


@ia_bp.route('/pessoas/remover/<int:pessoa_id>', methods=['POST'])
@login_required
def remover_pessoa(pessoa_id: int):
    """Remove uma pessoa cadastrada."""
    db = current_app.config['DB_PATH']
    pessoas_path = current_app.config['PESSOAS_PATH']

    try:
        with get_conexao(db) as conn:
            row = conn.execute(
                "SELECT nome, foto_path FROM pessoas_cadastradas WHERE id=?",
                (pessoa_id,)
            ).fetchone()

            if row:
                conn.execute(
                    "DELETE FROM pessoas_cadastradas WHERE id=?",
                    (pessoa_id,)
                )
                filepath = os.path.join(pessoas_path, row['foto_path'])
                if os.path.exists(filepath):
                    os.remove(filepath)

                flash(f'Pessoa "{row["nome"]}" removida.', 'success')

        motor = obter_motor_ia()
        motor.recarregar_pessoas(db)

    except Exception as e:
        logger.error(f"Erro ao remover pessoa: {e}")
        flash(f'Erro: {e}', 'danger')

    return redirect(url_for('ia.listar_pessoas'))


@ia_bp.route('/pessoa_foto/<filename>')
@login_required
def servir_foto_pessoa(filename):
    """Serve a foto de referência de uma pessoa cadastrada."""
    pessoas_path = current_app.config['PESSOAS_PATH']
    filepath     = os.path.join(pessoas_path, filename)
    if not os.path.exists(filepath):
        abort(404)
    return send_file(filepath, mimetype='image/jpeg')


# ═══════════════════════════════════════════════════════════════════════════
#  ROTAS - Galeria de Rostos Detectados
# ═══════════════════════════════════════════════════════════════════════════
@ia_bp.route('/galeria')
@login_required
def galeria_rostos():
    """
    Galeria de rostos detectados pelas câmeras (conhecidos e desconhecidos).
    Suporta filtros por câmera, pessoa e status (conhecido/desconhecido).
    """
    db = current_app.config['DB_PATH']

    filtro_camera = request.args.get('camera', '').strip()
    filtro_status = request.args.get('status', '').strip()  # 'conhecido'/'desconhecido'

    cameras = []
    try:
        with get_conexao(db) as conn:
            rows = conn.execute(
                "SELECT id, nome FROM cameras ORDER BY nome"
            ).fetchall()
            cameras = [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Erro ao listar câmeras: {e}")

    rostos = []
    try:
        with get_conexao(db) as conn:
            query = """
                SELECT r.*, c.nome as camera_nome
                FROM rostos_detectados r
                JOIN cameras c ON r.camera_id = c.id
                WHERE 1=1
            """
            params = []

            if filtro_camera and filtro_camera.isdigit():
                query += " AND r.camera_id = ?"
                params.append(int(filtro_camera))

            if filtro_status == 'conhecido':
                query += " AND r.pessoa_id IS NOT NULL"
            elif filtro_status == 'desconhecido':
                query += " AND r.pessoa_id IS NULL"

            query += " ORDER BY r.data_deteccao DESC LIMIT 300"

            rows = conn.execute(query, params).fetchall()
            rostos = [dict(r) for r in rows]

    except Exception as e:
        logger.error(f"Erro ao listar galeria de rostos: {e}")

    return render_template(
        'ia_galeria.html',
        rostos=rostos,
        cameras=cameras,
        filtro_camera=filtro_camera,
        filtro_status=filtro_status
    )


@ia_bp.route('/rosto_evento/<filename>')
@login_required
def servir_rosto_evento(filename):
    """Serve a imagem de um rosto detectado (galeria)."""
    rostos_path = current_app.config['ROSTOS_PATH']
    filepath    = os.path.join(rostos_path, filename)
    if not os.path.exists(filepath):
        abort(404)
    return send_file(filepath, mimetype='image/jpeg')


@ia_bp.route('/rosto_evento/remover/<int:rosto_id>', methods=['POST'])
@login_required
def remover_rosto_evento(rosto_id: int):
    """Remove um registro da galeria de rostos."""
    db = current_app.config['DB_PATH']
    rostos_path = current_app.config['ROSTOS_PATH']

    try:
        with get_conexao(db) as conn:
            row = conn.execute(
                "SELECT foto_rosto FROM rostos_detectados WHERE id=?",
                (rosto_id,)
            ).fetchone()
            if row:
                conn.execute(
                    "DELETE FROM rostos_detectados WHERE id=?", (rosto_id,)
                )
                filepath = os.path.join(rostos_path, row['foto_rosto'])
                if os.path.exists(filepath):
                    os.remove(filepath)
        flash('Registro removido da galeria.', 'success')
    except Exception as e:
        logger.error(f"Erro ao remover rosto: {e}")
        flash(f'Erro: {e}', 'danger')

    return redirect(url_for('ia.galeria_rostos'))


# ═══════════════════════════════════════════════════════════════════════════
#  FUNÇÃO AUXILIAR - Salvar rosto detectado (chamada pelo streaming.py)
# ═══════════════════════════════════════════════════════════════════════════
def salvar_rosto_detectado(db_path: str, rostos_path: str,
                           camera_id: int, evento_id: Optional[int],
                           info_rosto: dict) -> bool:
    """
    Salva no disco + banco um rosto detectado (conhecido ou desconhecido).
    Usado para montar a galeria de rostos identificados.
    """
    try:
        foto_rosto = info_rosto.get('foto_rosto')
        if foto_rosto is None or foto_rosto.size == 0:
            return False

        ts       = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"rosto_{camera_id}_{ts}.jpg"
        filepath = os.path.join(rostos_path, filename)

        cv2.imwrite(filepath, foto_rosto, [cv2.IMWRITE_JPEG_QUALITY, 85])

        with get_conexao(db_path) as conn:
            conn.execute(
                """INSERT INTO rostos_detectados
                   (camera_id, evento_id, pessoa_id, nome_identificado,
                    confianca, foto_rosto)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (camera_id, evento_id, info_rosto.get('pessoa_id'),
                 info_rosto.get('nome', 'Desconhecido'),
                 info_rosto.get('confianca', 0.0), filename)
            )

        del foto_rosto
        return True

    except Exception as e:
        logger.error(f"Erro ao salvar rosto detectado: {e}")
        return False
