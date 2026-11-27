#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Servidor Principal
Sistema de Monitoramento de Câmeras IP com Detecção de Movimento
+ Inteligência Artificial: Detecção de Objetos e Reconhecimento Facial
+ Controle PTZ: Auto-Calibração, Presets e Patrulha Automática

CORREÇÃO (compilação Nuitka --standalone):
Antes, BASE_DIR era calculado via os.path.dirname(os.path.abspath(__file__)),
o que funciona em desenvolvimento, mas quando o sistema roda como binário
compilado o importante é usar sys.argv[0] (caminho real do executável),
nunca nenhuma pasta temporária de extração.

Além disso, a pasta 'dados/' agora pode ficar FORA da pasta onde o
executável está instalado — controlado pela variável de ambiente
RRFS_DADOS_DIR (definida pelo serviço systemd em produção). Isso permite
atualizar o binário (pasta 'app/') sem nunca tocar nas gravações,
configurações e banco de dados do usuário.

Em desenvolvimento (rodando "python servidor.py" direto, sem a env var
definida), o comportamento é idêntico ao de antes: dados/ fica dentro da
mesma pasta do servidor.py.
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

# ── Detecção de BASE_DIR (dev vs binário compilado via Nuitka) ───────────
# Nuitka injeta a variável global '__compiled__' no módulo principal
# quando o código está rodando como binário compilado. Nesse caso,
# sys.argv[0] aponta para o executável real na pasta de instalação
# (nunca para uma pasta temporária) — diferente de __file__, que em
# alguns empacotadores pode apontar para um local instável.
if '__compiled__' in globals():
    BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, BASE_DIR)

# ── Pasta de dados: pode ficar FORA da pasta do binário (produção) ───────
# Em produção, o serviço systemd define RRFS_DADOS_DIR apontando para
# /DATA/AppData/RRFS_Cameras/dados — assim uma atualização do binário
# (pasta 'app/') nunca apaga gravações, configurações ou pessoas
# cadastradas. Em desenvolvimento, sem a env var definida, cai para
# BASE_DIR/dados como sempre foi.
DADOS_DIR = os.environ.get('RRFS_DADOS_DIR', os.path.join(BASE_DIR, 'dados'))

DIRETORIOS = [
    os.path.join(DADOS_DIR, 'config'),
    os.path.join(DADOS_DIR, 'logs'),
    os.path.join(DADOS_DIR, 'gravacoes'),
    os.path.join(DADOS_DIR, 'thumbs'),
    os.path.join(DADOS_DIR, 'pessoas'),
    os.path.join(DADOS_DIR, 'rostos'),
    os.path.join(DADOS_DIR, 'modelos_ia'),
    os.path.join(BASE_DIR, 'templates'),
    os.path.join(BASE_DIR, 'static'),
]
for d in DIRETORIOS:
    os.makedirs(d, exist_ok=True)

LOG_FILE = os.path.join(DADOS_DIR, 'logs', 'sistema.log')

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

def _carregar_secret_key() -> str:
    """Chave de sessão persistente: sessões sobrevivem a reinícios."""
    caminho = os.path.join(DADOS_DIR, 'config', 'secret.key')
    try:
        if os.path.exists(caminho):
            with open(caminho, 'r', encoding='utf-8') as fh:
                chave = fh.read().strip()
            if len(chave) >= 32:
                return chave
        chave = os.urandom(32).hex()
        with open(caminho, 'w', encoding='utf-8') as fh:
            fh.write(chave)
        try:
            os.chmod(caminho, 0o600)
        except Exception:
            pass
        return chave
    except Exception as e:
        logger.warning(f"Não foi possível persistir a secret key: {e}")
        return os.urandom(32).hex()


app.secret_key = _carregar_secret_key()
app.config['BASE_DIR']             = BASE_DIR
app.config['DADOS_DIR']            = DADOS_DIR
app.config['DB_PATH']              = os.path.join(DADOS_DIR, 'config', 'sistema.db')
app.config['GRAVACOES_PATH']       = os.path.join(DADOS_DIR, 'gravacoes')
app.config['THUMBS_PATH']          = os.path.join(DADOS_DIR, 'thumbs')
app.config['PESSOAS_PATH']         = os.path.join(DADOS_DIR, 'pessoas')
app.config['ROSTOS_PATH']          = os.path.join(DADOS_DIR, 'rostos')
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
    logger.info(f"  BASE_DIR  : {BASE_DIR}")
    logger.info(f"  DADOS_DIR : {DADOS_DIR}")
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

    try:
        # Servidor de produção (pip install waitress). Cada stream MJPEG
        # ocupa uma thread, por isso o número alto.
        from waitress import serve
        logger.info("Servidor WSGI: waitress")
        serve(app, host='0.0.0.0', port=2901, threads=64)
    except ImportError:
        logger.warning(
            "waitress não instalado — usando servidor de desenvolvimento "
            "do Flask. Para produção: pip install waitress"
        )
        app.run(
            host='0.0.0.0',
            port=2901,
            debug=False,
            threaded=True,
            use_reloader=False
        )
