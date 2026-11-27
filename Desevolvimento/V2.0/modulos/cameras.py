#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Módulo de Gerenciamento de Câmeras
"""

import logging
from flask import (Blueprint, render_template, request,
                   redirect, url_for, flash, current_app, jsonify)
from flask_login import login_required, current_user

from modulos.database import get_conexao
from modulos.streaming import (adicionar_stream, remover_stream,
                                status_stream, listar_streams,
                                atualizar_config_stream)

logger = logging.getLogger('RRFS.cameras')
cameras_bp = Blueprint('cameras', __name__)


def montar_url_rtsp(ip: str, porta: int,
                    usuario: str, senha: str) -> str:
    """Monta URL RTSP."""
    if usuario and senha:
        return f"rtsp://{usuario}:{senha}@{ip}:{porta}/live/ch00_0"
    return f"rtsp://{ip}:{porta}/live/ch00_0"


# ── Dashboard ──────────────────────────────────────────────────────────────
@cameras_bp.route('/dashboard')
@login_required
def dashboard():
    db = current_app.config['DB_PATH']
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


# ── Configurações ──────────────────────────────────────────────────────────
@cameras_bp.route('/configuracoes', methods=['GET', 'POST'])
@login_required
def configuracoes():
    db = current_app.config['DB_PATH']

    # Atualiza limite de armazenamento
    if request.method == 'POST' and 'max_armazenamento' in request.form:
        try:
            max_gb = int(request.form.get('max_armazenamento', 10))
            if max_gb < 1:
                max_gb = 1

            with get_conexao(db) as conn:
                conn.execute(
                    "UPDATE configuracoes SET valor = ? WHERE chave = 'max_armazenamento_gb'",
                    (str(max_gb),)
                )
            flash(f'Limite de armazenamento atualizado: {max_gb}GB', 'success')
        except Exception as e:
            flash(f'Erro ao atualizar limite: {e}', 'danger')

    cameras = []
    max_armazenamento = 10

    try:
        with get_conexao(db) as conn:
            rows = conn.execute("SELECT * FROM cameras ORDER BY id").fetchall()
            cameras = [dict(r) for r in rows]

            max_row = conn.execute(
                "SELECT valor FROM configuracoes WHERE chave = 'max_armazenamento_gb'"
            ).fetchone()
            if max_row:
                max_armazenamento = int(max_row['valor'])
    except Exception as e:
        logger.error(f"Erro ao listar câmeras: {e}")

    for cam in cameras:
        cam['online'] = status_stream(cam['id'])

    return render_template('configuracoes.html',
                           cameras=cameras,
                           max_armazenamento=max_armazenamento,
                           usuario=current_user.nome)


# ── Adicionar câmera ───────────────────────────────────────────────────────
@cameras_bp.route('/camera/adicionar', methods=['POST'])
@login_required
def adicionar_camera():
    db = current_app.config['DB_PATH']

    nome = request.form.get('nome', '').strip()
    ip = request.form.get('ip', '').strip()
    porta = request.form.get('porta', '554').strip()
    usuario = request.form.get('usuario', '').strip()
    senha = request.form.get('senha', '').strip()

    if not nome or not ip:
        flash('Nome e IP são obrigatórios.', 'danger')
        return redirect(url_for('cameras.configuracoes'))

    try:
        porta = int(porta)
    except:
        flash('Porta inválida.', 'danger')
        return redirect(url_for('cameras.configuracoes'))

    try:
        with get_conexao(db) as conn:
            cursor = conn.execute(
                """INSERT INTO cameras (nome, ip, porta, usuario, senha, gravar, sensibilidade)
                   VALUES (?, ?, ?, ?, ?, 0, 50)""",
                (nome, ip, porta, usuario, senha)
            )
            cam_id = cursor.lastrowid

        logger.info(f"Câmera adicionada: '{nome}' ID={cam_id}")

        url = montar_url_rtsp(ip, porta, usuario, senha)
        adicionar_stream(
            cam_id, nome, url, False, 50,
            current_app.config['GRAVACOES_PATH'],
            current_app.config['THUMBS_PATH']
        )

        flash(f'Câmera "{nome}" adicionada!', 'success')

    except Exception as e:
        logger.error(f"Erro ao adicionar câmera: {e}")
        flash(f'Erro: {e}', 'danger')

    return redirect(url_for('cameras.configuracoes'))


# ── Remover câmera ─────────────────────────────────────────────────────────
@cameras_bp.route('/camera/remover/<int:cam_id>', methods=['POST'])
@login_required
def remover_camera(cam_id: int):
    db = current_app.config['DB_PATH']
    try:
        remover_stream(cam_id)

        with get_conexao(db) as conn:
            row = conn.execute(
                "SELECT nome FROM cameras WHERE id = ?", (cam_id,)
            ).fetchone()
            if row:
                nome = row['nome']
                conn.execute("DELETE FROM cameras WHERE id = ?", (cam_id,))
                logger.info(f"Câmera removida: '{nome}' ID={cam_id}")
                flash(f'Câmera "{nome}" removida.', 'success')
    except Exception as e:
        logger.error(f"Erro ao remover câmera {cam_id}: {e}")
        flash('Erro ao remover câmera.', 'danger')

    return redirect(url_for('cameras.configuracoes'))


# ── Atualizar Gravação ─────────────────────────────────────────────────────
@cameras_bp.route('/camera/gravar/<int:cam_id>', methods=['POST'])
@login_required
def atualizar_gravacao(cam_id: int):
    db = current_app.config['DB_PATH']

    gravar = request.form.get('gravar') == '1'
    sensibilidade = int(request.form.get('sensibilidade', 50))

    try:
        with get_conexao(db) as conn:
            conn.execute(
                "UPDATE cameras SET gravar = ?, sensibilidade = ? WHERE id = ?",
                (1 if gravar else 0, sensibilidade, cam_id)
            )

        atualizar_config_stream(cam_id, gravar, sensibilidade)

        status = "ativada" if gravar else "desativada"
        flash(f'Gravação {status} (sensibilidade: {sensibilidade}%)', 'success')

    except Exception as e:
        logger.error(f"Erro ao atualizar gravação: {e}")
        flash('Erro ao atualizar configuração.', 'danger')

    return redirect(url_for('cameras.configuracoes'))


# ── Status API ─────────────────────────────────────────────────────────────
@cameras_bp.route('/api/status')
@login_required
def api_status():
    streams = listar_streams()
    return jsonify(streams)


@cameras_bp.route('/api/status/<int:cam_id>')
@login_required
def api_status_camera(cam_id: int):
    online = status_stream(cam_id)
    return jsonify({'id': cam_id, 'online': online})
