#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Servidor Principal
Sistema de Monitoramento de Câmeras IP com Detecção de Movimento
+ Inteligência Artificial: Detecção de Objetos e Reconhecimento Facial
+ Controle PTZ: Auto-Calibração, Presets e Patrulha Automática
"""

import os
import sys
import logging
import signal
import threading
from datetime import datetime

os.environ['OPENCV_LOG_LEVEL']      = 'ERROR'
os.environ['OPENCV_FFMPEG_LOGLEVEL'] = '0'

from flask import Flask, redirect, url_for
from flask_login import LoginManager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

DIRETORIOS = [
    os.path.join(BASE_DIR, 'dados', 'config'),
    os.path.join(BASE_DIR, 'dados', 'logs'),
    os.path.join(BASE_DIR, 'dados', 'gravacoes'),
    os.path.join(BASE_DIR, 'dados', 'thumbs'),
    os.path.join(BASE_DIR, 'dados', 'pessoas'),
    os.path.join(BASE_DIR, 'dados', 'rostos'),
    os.path.join(BASE_DIR, 'modulos'),
    os.path.join(BASE_DIR, 'templates'),
    os.path.join(BASE_DIR, 'static'),
]
for d in DIRETORIOS:
    os.makedirs(d, exist_ok=True)

LOG_FILE = os.path.join(BASE_DIR, 'dados', 'logs', 'sistema.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('RRFS')

from modulos.database     import iniciar_banco
from modulos.auth         import auth_bp, login_manager_init, usuario_existe
from modulos.cameras      import cameras_bp
from modulos.streaming    import streaming_bp
from modulos.eventos      import eventos_bp
from modulos.ia_deteccao  import ia_bp, obter_motor_ia
from modulos.ptz_controle import ptz_bp, ptz_disponivel, iniciar_patrulhas_salvas, encerrar_todas_patrulhas

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, 'templates'),
    static_folder=os.path.join(BASE_DIR, 'static')
)

app.secret_key = os.urandom(32).hex()
app.config['BASE_DIR']             = BASE_DIR
app.config['DB_PATH']              = os.path.join(BASE_DIR, 'dados', 'config', 'sistema.db')
app.config['GRAVACOES_PATH']       = os.path.join(BASE_DIR, 'dados', 'gravacoes')
app.config['THUMBS_PATH']          = os.path.join(BASE_DIR, 'dados', 'thumbs')
app.config['PESSOAS_PATH']         = os.path.join(BASE_DIR, 'dados', 'pessoas')
app.config['ROSTOS_PATH']          = os.path.join(BASE_DIR, 'dados', 'rostos')
app.config['MAX_CONTENT_LENGTH']   = 500 * 1024 * 1024


@app.context_processor
def inject_datetime():
    return {'now': datetime.now}


login_manager = LoginManager()
login_manager_init(login_manager, app)

app.register_blueprint(auth_bp)
app.register_blueprint(cameras_bp)
app.register_blueprint(streaming_bp)
app.register_blueprint(eventos_bp)
app.register_blueprint(ia_bp)
app.register_blueprint(ptz_bp)


@app.route('/')
def index():
    if not usuario_existe(app.config['DB_PATH']):
        return redirect(url_for('auth.setup'))
    return redirect(url_for('auth.login'))


def shutdown_handler(signum, frame):
    logger.info("Sinal de encerramento recebido. Finalizando RRFS Cameras...")
    from modulos.streaming import encerrar_todos_streams
    encerrar_todos_streams()
    encerrar_todas_patrulhas()
    sys.exit(0)


signal.signal(signal.SIGINT,  shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)


def _aquecer_motor_ia():
    try:
        motor = obter_motor_ia()
        info  = motor.info_hardware()
        logger.info("=" * 50)
        logger.info("  🧠 Motor de Inteligência Artificial")
        logger.info(f"  Device ativo : {info['device'].upper()}")
        logger.info(f"  GPU detectada: {info['gpu_disponivel']}")
        if info['gpu_disponivel']:
            logger.info(f"  GPU          : {info.get('gpu_nome', '?')}")
        logger.info(f"  YOLO (objetos)    : {'OK' if info['yolo_ok']  else 'INDISPONÍVEL'}")
        logger.info(f"  Facial (reconhec.): {'OK' if info['face_ok']  else 'INDISPONÍVEL'}")
        logger.info(f"  PTZ (ONVIF)       : {'OK' if ptz_disponivel() else 'INDISPONÍVEL'}")
        logger.info("=" * 50)
    except Exception as e:
        logger.error(f"Erro ao inicializar motor de IA: {e}", exc_info=True)


if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("  RRFS Cameras - Iniciando servidor...")
    logger.info("=" * 50)

    iniciar_banco(app.config['DB_PATH'])
    logger.info(f"Banco de dados: {app.config['DB_PATH']}")

    t_ia = threading.Thread(target=_aquecer_motor_ia, daemon=True, name='IA-Warmup')
    t_ia.start()

    from modulos.eventos import iniciar_limpeza_automatica
    t_limpeza = threading.Thread(
        target=iniciar_limpeza_automatica,
        args=(app.config['DB_PATH'], app.config['GRAVACOES_PATH']),
        daemon=True, name='AutoClean'
    )
    t_limpeza.start()

    from modulos.streaming import iniciar_streams_salvos
    t_streams = threading.Thread(
        target=iniciar_streams_salvos,
        args=(app.config['DB_PATH'], app.config['GRAVACOES_PATH'],
              app.config['THUMBS_PATH'], app.config['ROSTOS_PATH']),
        daemon=True, name='StreamManager'
    )
    t_streams.start()

    t_patrulhas = threading.Thread(
        target=iniciar_patrulhas_salvas,
        args=(app.config['DB_PATH'],),
        daemon=True, name='PatrolManager'
    )
    t_patrulhas.start()

    logger.info("Servidor disponível em: http://0.0.0.0:2901")

    app.run(
        host='0.0.0.0',
        port=2901,
        debug=False,
        threaded=True,
        use_reloader=False
    )
