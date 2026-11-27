#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Módulo de Gerenciamento de Eventos
Visualização, reprodução e limpeza automática de gravações
"""

import os
import logging
import time
import threading
from flask import (Blueprint, render_template, send_file,
                   jsonify, current_app, abort)
from flask_login import login_required

from modulos.database import get_conexao

logger = logging.getLogger('RRFS.eventos')
eventos_bp = Blueprint('eventos', __name__)


# ── Página de Eventos ──────────────────────────────────────────────────────
@eventos_bp.route('/eventos')
@login_required
def listar_eventos():
    """Página principal de visualização de eventos gravados."""
    db = current_app.config['DB_PATH']
    eventos = []

    try:
        with get_conexao(db) as conn:
            rows = conn.execute(
                """SELECT e.*, c.nome as camera_nome
                   FROM eventos e
                   JOIN cameras c ON e.camera_id = c.id
                   ORDER BY e.data_evento DESC
                   LIMIT 500"""
            ).fetchall()
            eventos = [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Erro ao listar eventos: {e}")

    return render_template('eventos.html', eventos=eventos)


# ── Servir Thumbnail ───────────────────────────────────────────────────────
@eventos_bp.route('/thumb/<filename>')
@login_required
def servir_thumb(filename):
    """Serve thumbnail de evento."""
    thumbs_path = current_app.config['THUMBS_PATH']
    filepath = os.path.join(thumbs_path, filename)

    if not os.path.exists(filepath):
        abort(404)

    return send_file(filepath, mimetype='image/jpeg')


# ── Servir Vídeo ───────────────────────────────────────────────────────────
@eventos_bp.route('/video/<filename>')
@login_required
def servir_video(filename):
    """Serve vídeo de evento para reprodução no navegador."""
    gravacoes_path = current_app.config['GRAVACOES_PATH']
    filepath = os.path.join(gravacoes_path, filename)

    if not os.path.exists(filepath):
        abort(404)

    return send_file(
        filepath,
        mimetype='video/mp4',
        as_attachment=False,
        download_name=filename
    )


# ── Download de Vídeo ──────────────────────────────────────────────────────
@eventos_bp.route('/download/<filename>')
@login_required
def download_video(filename):
    """Download de vídeo."""
    gravacoes_path = current_app.config['GRAVACOES_PATH']
    filepath = os.path.join(gravacoes_path, filename)

    if not os.path.exists(filepath):
        abort(404)

    return send_file(
        filepath,
        mimetype='video/mp4',
        as_attachment=True,
        download_name=filename
    )


# ── API de Estatísticas ────────────────────────────────────────────────────
@eventos_bp.route('/api/estatisticas')
@login_required
def estatisticas():
    """Retorna estatísticas de armazenamento."""
    db = current_app.config['DB_PATH']
    gravacoes_path = current_app.config['GRAVACOES_PATH']

    try:
        with get_conexao(db) as conn:
            # Total de eventos
            total = conn.execute("SELECT COUNT(*) as total FROM eventos").fetchone()['total']

            # Tamanho total
            tamanho = conn.execute(
                "SELECT SUM(tamanho_bytes) as total FROM eventos"
            ).fetchone()['total'] or 0

            # Limite configurado
            max_gb = float(conn.execute(
                "SELECT valor FROM configuracoes WHERE chave = 'max_armazenamento_gb'"
            ).fetchone()['valor'])

        # Tamanho real no disco
        tamanho_real = 0
        if os.path.exists(gravacoes_path):
            for f in os.listdir(gravacoes_path):
                fp = os.path.join(gravacoes_path, f)
                if os.path.isfile(fp):
                    tamanho_real += os.path.getsize(fp)

        return jsonify({
            'total_eventos': total,
            'tamanho_gb': round(tamanho_real / 1024 / 1024 / 1024, 2),
            'limite_gb': max_gb,
            'percentual': round((tamanho_real / 1024 / 1024 / 1024) / max_gb * 100, 1)
        })

    except Exception as e:
        logger.error(f"Erro ao obter estatísticas: {e}")
        return jsonify({'erro': str(e)}), 500


# ── Limpeza Automática ─────────────────────────────────────────────────────
def calcular_espaco_usado(gravacoes_path: str) -> int:
    """Retorna espaço usado em bytes."""
    total = 0
    if os.path.exists(gravacoes_path):
        for f in os.listdir(gravacoes_path):
            fp = os.path.join(gravacoes_path, f)
            if os.path.isfile(fp):
                total += os.path.getsize(fp)
    return total


def limpar_videos_antigos(db_path: str, gravacoes_path: str, thumbs_path: str):
    """Remove vídeos mais antigos até ficar abaixo do limite."""
    try:
        with get_conexao(db_path) as conn:
            max_gb = float(conn.execute(
                "SELECT valor FROM configuracoes WHERE chave = 'max_armazenamento_gb'"
            ).fetchone()['valor'])

            max_bytes = int(max_gb * 1024 * 1024 * 1024)
            usado = calcular_espaco_usado(gravacoes_path)

            if usado <= max_bytes:
                return  # Ainda tem espaço

            logger.warning(
                f"Armazenamento cheio: {usado/1024/1024/1024:.2f}GB / {max_gb}GB"
            )

            # Busca eventos mais antigos
            rows = conn.execute(
                "SELECT * FROM eventos ORDER BY data_evento ASC"
            ).fetchall()

            removidos = 0
            for row in rows:
                if usado <= max_bytes * 0.9:  # Deixa 10% de margem
                    break

                # Remove arquivos
                video_path = os.path.join(gravacoes_path, row['arquivo_video'])
                thumb_path = os.path.join(thumbs_path, row['arquivo_thumb'])

                if os.path.exists(video_path):
                    tamanho = os.path.getsize(video_path)
                    os.remove(video_path)
                    usado -= tamanho
                    removidos += 1

                if os.path.exists(thumb_path):
                    os.remove(thumb_path)

                # Remove do banco
                conn.execute("DELETE FROM eventos WHERE id = ?", (row['id'],))

            conn.commit()
            logger.info(f"Limpeza automática: {removidos} vídeo(s) removido(s)")

    except Exception as e:
        logger.error(f"Erro na limpeza automática: {e}")


def iniciar_limpeza_automatica(db_path: str, gravacoes_path: str):
    """Thread de limpeza automática (roda a cada 10 minutos)."""
    thumbs_path = os.path.join(os.path.dirname(gravacoes_path), 'thumbs')

    while True:
        time.sleep(600)  # 10 minutos
        try:
            limpar_videos_antigos(db_path, gravacoes_path, thumbs_path)
        except Exception as e:
            logger.error(f"Erro no loop de limpeza: {e}")
