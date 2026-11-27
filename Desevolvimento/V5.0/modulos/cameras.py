#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Módulo de Gerenciamento de Câmeras
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
                                atualizar_zona_stream)

logger     = logging.getLogger('RRFS.cameras')
cameras_bp = Blueprint('cameras', __name__)


def montar_url_rtsp(ip, porta, usuario, senha, rtsp_path=''):
    """
    Monta URL RTSP.
    safe='%' → preserva % já codificado na senha (ex: Homesick1%F)
    """
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
        cam['online'] = status_stream(cam['id'])

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
        cam['online']     = status_stream(cam['id'])
        cam['tem_zona']   = bool(cam.get('zona_pontos', ''))
        cam['transporte'] = cam.get('transporte', 'auto')

    return render_template('configuracoes.html',
                           cameras=cameras,
                           max_armazenamento=max_armazenamento,
                           usuario=current_user.nome)


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

    # Valida transporte
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
                    rtsp_path, transporte, gravar, sensibilidade, zona_pontos)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, 50, '')""",
                (nome, ip, porta_int, usuario, senha, rtsp_path, transporte)
            )
            cam_id = cursor.lastrowid

        url = montar_url_rtsp(ip, porta_int, usuario, senha, rtsp_path)

        # Log seguro
        url_log = montar_url_rtsp(ip, porta_int, usuario, '****', rtsp_path)
        logger.info(
            f"[Cam {cam_id}] Adicionada · "
            f"transporte={transporte} · url={url_log}"
        )

        adicionar_stream(
            cam_id, nome, url, False, 50,
            current_app.config['GRAVACOES_PATH'],
            current_app.config['THUMBS_PATH'],
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
        logger.info(f"[Cam {cam_id}] Zona atualizada: {msg}")
        return jsonify({'ok': True, 'mensagem': msg})

    except Exception as e:
        logger.error(f"Erro ao salvar zona: {e}")
        return jsonify({'ok': False, 'erro': str(e)}), 500


# ── API Status ────────────────────────────────────────────────────────────
@cameras_bp.route('/api/status')
@login_required
def api_status():
    return jsonify(listar_streams())


@cameras_bp.route('/api/status/<int:cam_id>')
@login_required
def api_status_camera(cam_id: int):
    return jsonify({'id': cam_id, 'online': status_stream(cam_id)})
