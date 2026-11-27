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
                                status_stream, listar_streams)

logger = logging.getLogger('RRFS.cameras')
cameras_bp = Blueprint('cameras', __name__)


def montar_url_rtsp(ip: str, porta: int,
                    usuario: str, senha: str) -> str:
    """Monta URL RTSP para câmeras iCSee / padrão."""
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

    # Adiciona status de cada câmera
    for cam in cameras:
        cam['online'] = status_stream(cam['id'])

    return render_template('dashboard.html',
                           cameras=cameras,
                           usuario=current_user.nome)


# ── Configurações ──────────────────────────────────────────────────────────
@cameras_bp.route('/configuracoes')
@login_required
def configuracoes():
    db = current_app.config['DB_PATH']
    cameras = []
    try:
        with get_conexao(db) as conn:
            rows = conn.execute(
                "SELECT * FROM cameras ORDER BY id"
            ).fetchall()
            cameras = [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Erro ao listar câmeras: {e}")

    for cam in cameras:
        cam['online'] = status_stream(cam['id'])

    return render_template('configuracoes.html',
                           cameras=cameras,
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

    # Validações
    if not nome:
        flash('Nome da câmera é obrigatório.', 'danger')
        return redirect(url_for('cameras.configuracoes'))
    if not ip:
        flash('IP da câmera é obrigatório.', 'danger')
        return redirect(url_for('cameras.configuracoes'))

    try:
        porta = int(porta)
        if not (1 <= porta <= 65535):
            raise ValueError
    except ValueError:
        flash('Porta inválida. Use um número entre 1 e 65535.', 'danger')
        return redirect(url_for('cameras.configuracoes'))

    try:
        with get_conexao(db) as conn:
            cursor = conn.execute(
                """INSERT INTO cameras (nome, ip, porta, usuario, senha)
                   VALUES (?, ?, ?, ?, ?)""",
                (nome, ip, porta, usuario, senha)
            )
            cam_id = cursor.lastrowid

        logger.info(f"Câmera adicionada: '{nome}' [{ip}:{porta}] ID={cam_id}")

        # Inicia stream imediatamente
        url = montar_url_rtsp(ip, porta, usuario, senha)
        adicionar_stream(cam_id, nome, url)

        flash(f'Câmera "{nome}" adicionada com sucesso!', 'success')

    except Exception as e:
        logger.error(f"Erro ao adicionar câmera: {e}")
        flash(f'Erro ao adicionar câmera: {e}', 'danger')

    return redirect(url_for('cameras.configuracoes'))


# ── Remover câmera ─────────────────────────────────────────────────────────
@cameras_bp.route('/camera/remover/<int:cam_id>', methods=['POST'])
@login_required
def remover_camera(cam_id: int):
    db = current_app.config['DB_PATH']
    try:
        # Para o stream
        remover_stream(cam_id)

        with get_conexao(db) as conn:
            row = conn.execute(
                "SELECT nome FROM cameras WHERE id = ?", (cam_id,)
            ).fetchone()
            if row:
                nome = row['nome']
                conn.execute(
                    "DELETE FROM cameras WHERE id = ?", (cam_id,)
                )
                logger.info(f"Câmera removida: '{nome}' ID={cam_id}")
                flash(f'Câmera "{nome}" removida.', 'success')
    except Exception as e:
        logger.error(f"Erro ao remover câmera {cam_id}: {e}")
        flash('Erro ao remover câmera.', 'danger')

    return redirect(url_for('cameras.configuracoes'))


# ── Status via API ─────────────────────────────────────────────────────────
@cameras_bp.route('/api/status')
@login_required
def api_status():
    """Retorna status de todas as câmeras em JSON."""
    streams = listar_streams()
    return jsonify(streams)


@cameras_bp.route('/api/status/<int:cam_id>')
@login_required
def api_status_camera(cam_id: int):
    """Retorna status de uma câmera específica."""
    online = status_stream(cam_id)
    return jsonify({'id': cam_id, 'online': online})
