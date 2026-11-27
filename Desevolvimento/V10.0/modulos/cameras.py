#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Módulo de Gerenciamento de Câmeras
+ Configuração de IA por câmera + Integração com status PTZ

CORREÇÃO: a tela de IA ("Quando gravar") agora tem apenas 2 opções —
"Qualquer Movimento" (que respeita as categorias marcadas nos
checkboxes de detecção, quando a IA está ativa) e "Pessoa Específica".
Os modos antigos 'pessoa'/'veiculo'/'animal' (redundantes com os
checkboxes) continuam sendo aceitos no backend por retrocompatibilidade,
mas são normalizados para 'movimento' ao abrir a tela de edição.
"""

import json
import logging
from urllib.parse import quote

from flask import (Blueprint, render_template, request,
                   redirect, url_for, flash, current_app, jsonify)
from flask_login import login_required, current_user

from modulos.database  import get_conexao
from modulos.streaming import (adicionar_stream, remover_stream,
                                status_stream, listar_streams,
                                atualizar_config_stream,
                                atualizar_zona_stream,
                                atualizar_config_ia_stream)
from modulos.ia_deteccao import (ia_objetos_disponivel,
                                 ia_facial_disponivel,
                                 obter_motor_ia)
from modulos.ptz_controle import patrulha_ativa, ptz_disponivel

logger     = logging.getLogger('RRFS.cameras')
cameras_bp = Blueprint('cameras', __name__)

# ── Valores legados de modo_gravacao que agora são controlados pelos
#    checkboxes de detecção (detectar_pessoa/veiculo/animal). Mantidos
#    aqui só para normalizar a exibição de câmeras configuradas antes
#    desta correção.
_MODOS_LEGADOS_CATEGORIA = ('pessoa', 'veiculo', 'animal')


def montar_url_rtsp(ip, porta, usuario, senha, rtsp_path=''):
    usuario_enc = quote(usuario, safe='%') if usuario else ''
    senha_enc   = quote(senha,   safe='%') if senha   else ''

    if usuario_enc and senha_enc:
        auth = f"{usuario_enc}:{senha_enc}@"
    elif usuario_enc:
        auth = f"{usuario_enc}@"
    else:
        auth = ""

    if rtsp_path:
        path = rtsp_path.strip().lstrip('/')
        return f"rtsp://{auth}{ip}/{path}"

    try:
        porta_int = int(porta) if porta else 554
    except Exception:
        porta_int = 554

    return f"rtsp://{auth}{ip}:{porta_int}/live/ch00_0"


# ── Dashboard ─────────────────────────────────────────────────────────────
@cameras_bp.route('/dashboard')
@login_required
def dashboard():
    db      = current_app.config['DB_PATH']
    cameras = []
    try:
        with get_conexao(db) as conn:
            rows = conn.execute(
                "SELECT * FROM cameras WHERE ativa = 1 ORDER BY id"
            ).fetchall()
            cameras = [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Erro ao listar câmeras: {e}")

    for cam in cameras:
        cam['online']         = status_stream(cam['id'])
        cam['ptz_patrulha']   = patrulha_ativa(cam['id'])

    return render_template('dashboard.html',
                           cameras=cameras,
                           usuario=current_user.nome)


# ── Configurações ─────────────────────────────────────────────────────────
@cameras_bp.route('/configuracoes', methods=['GET', 'POST'])
@login_required
def configuracoes():
    db = current_app.config['DB_PATH']

    if request.method == 'POST' and 'max_armazenamento' in request.form:
        try:
            max_gb = max(1, int(request.form.get('max_armazenamento', 10)))
            with get_conexao(db) as conn:
                conn.execute(
                    "UPDATE configuracoes SET valor=? "
                    "WHERE chave='max_armazenamento_gb'",
                    (str(max_gb),)
                )
            flash(f'Limite de armazenamento: {max_gb} GB', 'success')
        except Exception as e:
            flash(f'Erro: {e}', 'danger')

    cameras           = []
    max_armazenamento = 10
    try:
        with get_conexao(db) as conn:
            rows = conn.execute(
                "SELECT * FROM cameras ORDER BY id"
            ).fetchall()
            cameras = [dict(r) for r in rows]
            row = conn.execute(
                "SELECT valor FROM configuracoes "
                "WHERE chave='max_armazenamento_gb'"
            ).fetchone()
            if row:
                max_armazenamento = int(row['valor'])
    except Exception as e:
        logger.error(f"Erro: {e}")

    for cam in cameras:
        cam['online']            = status_stream(cam['id'])
        cam['tem_zona']          = bool(cam.get('zona_pontos', ''))
        cam['transporte']        = cam.get('transporte', 'auto')
        cam['ptz_patrulha_rodando'] = patrulha_ativa(cam['id'])

    motor_info = obter_motor_ia().info_hardware()

    return render_template('configuracoes.html',
                           cameras=cameras,
                           max_armazenamento=max_armazenamento,
                           usuario=current_user.nome,
                           ia_hardware=motor_info,
                           ptz_disponivel=ptz_disponivel())


# ── Adicionar câmera ──────────────────────────────────────────────────────
@cameras_bp.route('/camera/adicionar', methods=['POST'])
@login_required
def adicionar_camera():
    db         = current_app.config['DB_PATH']
    nome       = request.form.get('nome',       '').strip()
    ip         = request.form.get('ip',         '').strip()
    porta      = request.form.get('porta',      '').strip()
    usuario    = request.form.get('usuario',    '').strip()
    senha      = request.form.get('senha',      '').strip()
    rtsp_path  = request.form.get('rtsp_path',  '').strip()
    transporte = request.form.get('transporte', 'auto').strip()

    if transporte not in ('auto', 'tcp', 'udp'):
        transporte = 'auto'

    if not nome or not ip:
        flash('Nome e IP são obrigatórios.', 'danger')
        return redirect(url_for('cameras.configuracoes'))

    porta_int = 554
    if porta:
        try:
            porta_int = int(porta)
        except Exception:
            flash('Porta inválida.', 'danger')
            return redirect(url_for('cameras.configuracoes'))

    try:
        with get_conexao(db) as conn:
            cursor = conn.execute(
                """INSERT INTO cameras
                   (nome, ip, porta, usuario, senha,
                    rtsp_path, transporte, gravar, sensibilidade, zona_pontos,
                    ia_ativa, detectar_pessoa, detectar_veiculo,
                    detectar_animal, reconhecimento_facial,
                    confianca_minima, modo_gravacao)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, 50, '',
                           0, 1, 0, 0, 0, 0.5, 'movimento')""",
                (nome, ip, porta_int, usuario, senha, rtsp_path, transporte)
            )
            cam_id = cursor.lastrowid

        url = montar_url_rtsp(ip, porta_int, usuario, senha, rtsp_path)
        url_log = montar_url_rtsp(ip, porta_int, usuario, '****', rtsp_path)
        logger.info(
            f"[Cam {cam_id}] Adicionada · "
            f"transporte={transporte} · url={url_log}"
        )

        adicionar_stream(
            cam_id, nome, url, False, 50,
            current_app.config['GRAVACOES_PATH'],
            current_app.config['THUMBS_PATH'],
            current_app.config['ROSTOS_PATH'],
            '', transporte
        )
        flash(f'Câmera "{nome}" adicionada com sucesso!', 'success')
    except Exception as e:
        flash(f'Erro ao adicionar câmera: {e}', 'danger')
        logger.error(f"Erro ao adicionar câmera: {e}")

    return redirect(url_for('cameras.configuracoes'))


# ── Remover câmera ────────────────────────────────────────────────────────
@cameras_bp.route('/camera/remover/<int:cam_id>', methods=['POST'])
@login_required
def remover_camera(cam_id: int):
    db = current_app.config['DB_PATH']
    try:
        from modulos.ptz_controle import parar_patrulha
        parar_patrulha(cam_id)
        remover_stream(cam_id)
        with get_conexao(db) as conn:
            row = conn.execute(
                "SELECT nome FROM cameras WHERE id=?", (cam_id,)
            ).fetchone()
            if row:
                conn.execute(
                    "DELETE FROM cameras WHERE id=?", (cam_id,)
                )
                flash(f'Câmera "{row["nome"]}" removida.', 'success')
    except Exception as e:
        flash(f'Erro: {e}', 'danger')
    return redirect(url_for('cameras.configuracoes'))


# ── Atualizar gravação ────────────────────────────────────────────────────
@cameras_bp.route('/camera/gravar/<int:cam_id>', methods=['POST'])
@login_required
def atualizar_gravacao(cam_id: int):
    db            = current_app.config['DB_PATH']
    gravar        = request.form.get('gravar') == '1'
    sensibilidade = int(request.form.get('sensibilidade', 50))
    try:
        with get_conexao(db) as conn:
            conn.execute(
                "UPDATE cameras SET gravar=?, sensibilidade=? WHERE id=?",
                (1 if gravar else 0, sensibilidade, cam_id)
            )
        atualizar_config_stream(cam_id, gravar, sensibilidade)
        status = "ativada" if gravar else "desativada"
        flash(
            f'Gravação {status} · sensibilidade {sensibilidade}%',
            'success'
        )
    except Exception as e:
        flash(f'Erro: {e}', 'danger')
    return redirect(url_for('cameras.configuracoes'))


# ── Editor de Zona ────────────────────────────────────────────────────────
@cameras_bp.route('/camera/zona/<int:cam_id>')
@login_required
def editor_zona(cam_id: int):
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

    return render_template('zona_editor.html',
                           cam=cam,
                           usuario=current_user.nome)


# ── Salvar Zona (API) ─────────────────────────────────────────────────────
@cameras_bp.route('/camera/zona/<int:cam_id>/salvar', methods=['POST'])
@login_required
def salvar_zona(cam_id: int):
    db = current_app.config['DB_PATH']
    try:
        data   = request.get_json()
        pontos = data.get('pontos', [])

        if pontos and len(pontos) < 3:
            return jsonify({'ok': False, 'erro': 'Mínimo 3 pontos.'}), 400

        zona_json = json.dumps(pontos) if pontos else ''

        with get_conexao(db) as conn:
            conn.execute(
                "UPDATE cameras SET zona_pontos=? WHERE id=?",
                (zona_json, cam_id)
            )

        atualizar_zona_stream(cam_id, zona_json)
        msg = f"{len(pontos)} pontos salvos." if pontos else "Zona removida."
        return jsonify({'ok': True, 'mensagem': msg})

    except Exception as e:
        logger.error(f"Erro ao salvar zona: {e}")
        return jsonify({'ok': False, 'erro': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════
#  Inteligência Artificial - Configuração por Câmera
# ═══════════════════════════════════════════════════════════════════════════
@cameras_bp.route('/camera/ia/<int:cam_id>')
@login_required
def editor_ia(cam_id: int):
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

            pessoas = conn.execute(
                "SELECT id, nome FROM pessoas_cadastradas "
                "WHERE ativo=1 ORDER BY nome"
            ).fetchall()
            pessoas = [dict(p) for p in pessoas]
    except Exception as e:
        flash(f'Erro: {e}', 'danger')
        return redirect(url_for('cameras.configuracoes'))

    # ── Normaliza valores legados de modo_gravacao para exibição ────────
    # Câmeras configuradas antes da correção podem ter 'pessoa', 'veiculo'
    # ou 'animal' salvos no modo_gravacao. Esses valores agora são
    # controlados pelos checkboxes (detectar_pessoa/veiculo/animal), então
    # exibimos a tela já no modo unificado 'movimento' — os checkboxes
    # continuam refletindo corretamente o que estava marcado antes.
    if cam.get('modo_gravacao') in _MODOS_LEGADOS_CATEGORIA:
        cam['modo_gravacao'] = 'movimento'

    return render_template(
        'ia_config.html',
        cam=cam,
        pessoas=pessoas,
        usuario=current_user.nome,
        objetos_disponivel=ia_objetos_disponivel(),
        facial_disponivel=ia_facial_disponivel(),
        hardware=obter_motor_ia().info_hardware()
    )


@cameras_bp.route('/camera/ia/<int:cam_id>/salvar', methods=['POST'])
@login_required
def salvar_ia(cam_id: int):
    db = current_app.config['DB_PATH']

    ia_ativa               = request.form.get('ia_ativa')  == '1'
    detectar_pessoa        = request.form.get('detectar_pessoa')  == '1'
    detectar_veiculo       = request.form.get('detectar_veiculo') == '1'
    detectar_animal        = request.form.get('detectar_animal')  == '1'
    reconhecimento_facial  = request.form.get('reconhecimento_facial') == '1'
    modo_gravacao          = request.form.get('modo_gravacao', 'movimento')
    pessoa_alvo_raw        = request.form.get('pessoa_alvo_id', '').strip()

    try:
        confianca_minima = float(request.form.get('confianca_minima', 0.5))
        confianca_minima = max(0.1, min(0.95, confianca_minima))
    except Exception:
        confianca_minima = 0.5

    pessoa_alvo_id = int(pessoa_alvo_raw) if pessoa_alvo_raw.isdigit() else None

    # 'pessoa'/'veiculo'/'animal' mantidos apenas por retrocompatibilidade
    # (não são mais oferecidos na interface, que agora envia só
    # 'movimento' ou 'pessoa_especifica').
    modos_validos = ('movimento', 'pessoa', 'veiculo', 'animal',
                     'pessoa_especifica')
    if modo_gravacao not in modos_validos:
        modo_gravacao = 'movimento'

    try:
        with get_conexao(db) as conn:
            conn.execute(
                """UPDATE cameras SET
                   ia_ativa=?, detectar_pessoa=?, detectar_veiculo=?,
                   detectar_animal=?, reconhecimento_facial=?,
                   confianca_minima=?, modo_gravacao=?, pessoa_alvo_id=?
                   WHERE id=?""",
                (int(ia_ativa), int(detectar_pessoa), int(detectar_veiculo),
                 int(detectar_animal), int(reconhecimento_facial),
                 confianca_minima, modo_gravacao, pessoa_alvo_id, cam_id)
            )

        atualizar_config_ia_stream(
            cam_id,
            ia_ativa=ia_ativa,
            detectar_pessoa=detectar_pessoa,
            detectar_veiculo=detectar_veiculo,
            detectar_animal=detectar_animal,
            reconhecimento_facial=reconhecimento_facial,
            confianca_minima=confianca_minima,
            modo_gravacao=modo_gravacao,
            pessoa_alvo_id=pessoa_alvo_id
        )

        flash('Configuração de IA salva com sucesso!', 'success')
    except Exception as e:
        logger.error(f"Erro ao salvar config IA: {e}")
        flash(f'Erro: {e}', 'danger')

    return redirect(url_for('cameras.editor_ia', cam_id=cam_id))


# ── API Status ────────────────────────────────────────────────────────────
@cameras_bp.route('/api/status')
@login_required
def api_status():
    streams = listar_streams()
    for s in streams:
        s['ptz_patrulha'] = patrulha_ativa(s['id'])
    return jsonify(streams)


@cameras_bp.route('/api/status/<int:cam_id>')
@login_required
def api_status_camera(cam_id: int):
    return jsonify({'id': cam_id, 'online': status_stream(cam_id)})
