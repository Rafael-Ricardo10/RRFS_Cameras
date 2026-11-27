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
import shutil
import subprocess
from flask import (Blueprint, render_template, send_file,
                   jsonify, current_app, abort, Response, request)
from flask_login import login_required

from modulos.database import get_conexao

logger     = logging.getLogger('RRFS.eventos')
eventos_bp = Blueprint('eventos', __name__)

# ── FFmpeg ────────────────────────────────────────────────────────────────
_FFMPEG_PATH = shutil.which('ffmpeg')
_FFMPEG_OK   = _FFMPEG_PATH is not None


def converter_video_h264(filepath: str) -> bool:
    """
    Converte vídeo mp4v → H.264 usando FFmpeg.
    Retorna True se converteu com sucesso.
    """
    if not _FFMPEG_OK:
        return False

    filepath_tmp = filepath + '.tmp.mp4'
    try:
        cmd = [
            _FFMPEG_PATH,
            '-y',
            '-i', filepath,
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            '-crf', '23',
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
            '-an',
            filepath_tmp
        ]
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=120
        )
        if result.returncode != 0:
            erro = result.stderr.decode(errors='ignore')[-300:]
            logger.error(f"FFmpeg erro ao converter {filepath}: {erro}")
            if os.path.exists(filepath_tmp):
                os.remove(filepath_tmp)
            return False

        if not os.path.exists(filepath_tmp) or \
           os.path.getsize(filepath_tmp) < 10 * 1024:
            if os.path.exists(filepath_tmp):
                os.remove(filepath_tmp)
            return False

        os.replace(filepath_tmp, filepath)
        logger.info(
            f"Convertido para H.264: {os.path.basename(filepath)} "
            f"({os.path.getsize(filepath)/1024/1024:.1f} MB)"
        )
        return True

    except subprocess.TimeoutExpired:
        logger.error(f"Timeout ao converter {filepath}")
        if os.path.exists(filepath_tmp):
            os.remove(filepath_tmp)
        return False
    except Exception as e:
        logger.error(f"Erro ao converter {filepath}: {e}")
        if os.path.exists(filepath_tmp):
            os.remove(filepath_tmp)
        return False


def garantir_h264(filepath: str) -> str:
    """
    Garante que o vídeo está em H.264.
    Converte se necessário. Retorna o caminho do arquivo.
    """
    if not os.path.exists(filepath):
        return filepath

    # Verifica codec atual com ffprobe
    ffprobe = shutil.which('ffprobe')
    if ffprobe:
        try:
            result = subprocess.run(
                [
                    ffprobe, '-v', 'error',
                    '-select_streams', 'v:0',
                    '-show_entries', 'stream=codec_name',
                    '-of', 'default=noprint_wrappers=1:nokey=1',
                    filepath
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10
            )
            codec = result.stdout.decode().strip()
            if codec == 'h264':
                return filepath  # já está em H.264
            logger.info(
                f"Vídeo em {codec}, convertendo para H.264: "
                f"{os.path.basename(filepath)}"
            )
        except Exception:
            pass

    converter_video_h264(filepath)
    return filepath


# ── Página de Eventos ──────────────────────────────────────────────────────
@eventos_bp.route('/eventos')
@login_required
def listar_eventos():
    """Página principal de visualização de eventos gravados."""
    db     = current_app.config['DB_PATH']
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
    filepath    = os.path.join(thumbs_path, filename)

    if not os.path.exists(filepath):
        logger.warning(f"Thumbnail não encontrado: {filename}")
        abort(404)

    return send_file(filepath, mimetype='image/jpeg')


# ── Servir Vídeo com Range Request ─────────────────────────────────────────
@eventos_bp.route('/video/<filename>')
@login_required
def servir_video(filename):
    """
    Serve vídeo para reprodução no navegador.
    Suporta Range Requests (seek/scrubbing).
    Converte para H.264 automaticamente se FFmpeg disponível.
    """
    gravacoes_path = current_app.config['GRAVACOES_PATH']
    filepath       = os.path.join(gravacoes_path, filename)

    if not os.path.exists(filepath):
        logger.warning(f"Vídeo não encontrado: {filename}")
        abort(404)

    # Converte para H.264 se necessário
    garantir_h264(filepath)

    file_size = os.path.getsize(filepath)
    range_header = request.headers.get('Range', None)

    # ── Sem Range: envia arquivo completo ─────────────────────────────────
    if not range_header:
        response = Response(
            _stream_file(filepath),
            status=200,
            mimetype='video/mp4',
            direct_passthrough=True
        )
        response.headers['Content-Length']              = file_size
        response.headers['Accept-Ranges']               = 'bytes'
        response.headers['Cache-Control']               = 'no-cache'
        response.headers['Content-Disposition']         = \
            f'inline; filename="{filename}"'
        return response

    # ── Com Range: resposta parcial (206) ──────────────────────────────────
    byte1, byte2 = 0, None
    match = range_header.strip().replace('bytes=', '')
    parts = match.split('-')
    if parts[0]:
        byte1 = int(parts[0])
    if len(parts) > 1 and parts[1]:
        byte2 = int(parts[1])

    if byte2 is None or byte2 >= file_size:
        byte2 = file_size - 1

    length = byte2 - byte1 + 1

    response = Response(
        _stream_file(filepath, byte1, length),
        status=206,
        mimetype='video/mp4',
        direct_passthrough=True
    )
    response.headers['Content-Range']      = \
        f'bytes {byte1}-{byte2}/{file_size}'
    response.headers['Content-Length']     = length
    response.headers['Accept-Ranges']      = 'bytes'
    response.headers['Cache-Control']      = 'no-cache'
    response.headers['Content-Disposition'] = \
        f'inline; filename="{filename}"'
    return response


def _stream_file(filepath: str, start: int = 0,
                 length: int = None, chunk_size: int = 1024 * 256):
    """Gerador para servir arquivo em chunks com suporte a Range."""
    with open(filepath, 'rb') as f:
        f.seek(start)
        remaining = length
        while True:
            if remaining is not None:
                chunk = f.read(min(chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
                if remaining <= 0:
                    break
            else:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield chunk


# ── Download de Vídeo ──────────────────────────────────────────────────────
@eventos_bp.route('/download/<filename>')
@login_required
def download_video(filename):
    """Download de vídeo."""
    gravacoes_path = current_app.config['GRAVACOES_PATH']
    filepath       = os.path.join(gravacoes_path, filename)

    if not os.path.exists(filepath):
        logger.warning(f"Vídeo não encontrado para download: {filename}")
        abort(404)

    # Converte para H.264 antes do download se necessário
    garantir_h264(filepath)

    return send_file(
        filepath,
        mimetype='video/mp4',
        as_attachment=True,
        download_name=filename
    )


# ── Converter vídeo existente (API) ────────────────────────────────────────
@eventos_bp.route('/api/converter/<int:evento_id>', methods=['POST'])
@login_required
def converter_evento(evento_id: int):
    """Força conversão de um evento para H.264."""
    db             = current_app.config['DB_PATH']
    gravacoes_path = current_app.config['GRAVACOES_PATH']

    try:
        with get_conexao(db) as conn:
            row = conn.execute(
                "SELECT arquivo_video FROM eventos WHERE id = ?",
                (evento_id,)
            ).fetchone()

        if not row:
            return jsonify({'ok': False, 'erro': 'Evento não encontrado'}), 404

        filepath = os.path.join(gravacoes_path, row['arquivo_video'])
        if not os.path.exists(filepath):
            return jsonify({'ok': False, 'erro': 'Arquivo não encontrado'}), 404

        if not _FFMPEG_OK:
            return jsonify({
                'ok': False,
                'erro': 'FFmpeg não instalado. Execute: sudo apt install ffmpeg'
            }), 500

        ok = converter_video_h264(filepath)
        if ok:
            novo_tamanho = os.path.getsize(filepath)
            with get_conexao(db) as conn:
                conn.execute(
                    "UPDATE eventos SET tamanho_bytes=? WHERE id=?",
                    (novo_tamanho, evento_id)
                )
            return jsonify({
                'ok': True,
                'mensagem': 'Convertido para H.264 com sucesso',
                'tamanho_mb': round(novo_tamanho / 1024 / 1024, 1)
            })
        else:
            return jsonify({'ok': False, 'erro': 'Falha na conversão'}), 500

    except Exception as e:
        logger.error(f"Erro ao converter evento {evento_id}: {e}")
        return jsonify({'ok': False, 'erro': str(e)}), 500


# ── Converter todos os vídeos existentes (API) ─────────────────────────────
@eventos_bp.route('/api/converter_todos', methods=['POST'])
@login_required
def converter_todos():
    """Converte todos os vídeos existentes para H.264 em background."""
    if not _FFMPEG_OK:
        return jsonify({
            'ok': False,
            'erro': 'FFmpeg não instalado. Execute: sudo apt install ffmpeg'
        }), 500

    db             = current_app.config['DB_PATH']
    gravacoes_path = current_app.config['GRAVACOES_PATH']

    def _converter_bg():
        try:
            with get_conexao(db) as conn:
                rows = conn.execute(
                    "SELECT id, arquivo_video FROM eventos"
                ).fetchall()

            convertidos = 0
            for row in rows:
                filepath = os.path.join(gravacoes_path, row['arquivo_video'])
                if os.path.exists(filepath):
                    if converter_video_h264(filepath):
                        novo_tamanho = os.path.getsize(filepath)
                        with get_conexao(db) as conn:
                            conn.execute(
                                "UPDATE eventos SET tamanho_bytes=? WHERE id=?",
                                (novo_tamanho, row['id'])
                            )
                        convertidos += 1

            logger.info(f"Conversão em lote: {convertidos} vídeo(s) convertido(s)")
        except Exception as e:
            logger.error(f"Erro na conversão em lote: {e}")

    t = threading.Thread(target=_converter_bg, daemon=True, name='Converter')
    t.start()

    return jsonify({
        'ok': True,
        'mensagem': 'Conversão iniciada em background'
    })


# ── API de Estatísticas ────────────────────────────────────────────────────
@eventos_bp.route('/api/estatisticas')
@login_required
def estatisticas():
    """Retorna estatísticas de armazenamento."""
    db             = current_app.config['DB_PATH']
    gravacoes_path = current_app.config['GRAVACOES_PATH']

    try:
        with get_conexao(db) as conn:
            total = conn.execute(
                "SELECT COUNT(*) as total FROM eventos"
            ).fetchone()['total']

            max_gb = float(conn.execute(
                "SELECT valor FROM configuracoes "
                "WHERE chave = 'max_armazenamento_gb'"
            ).fetchone()['valor'])

        tamanho_real = 0
        if os.path.exists(gravacoes_path):
            for f in os.listdir(gravacoes_path):
                fp = os.path.join(gravacoes_path, f)
                if os.path.isfile(fp):
                    tamanho_real += os.path.getsize(fp)

        tamanho_gb = round(tamanho_real / 1024 / 1024 / 1024, 2)
        percentual = round((tamanho_gb / max_gb * 100), 1) \
                     if max_gb > 0 else 0

        return jsonify({
            'total_eventos': total,
            'tamanho_gb'   : tamanho_gb,
            'limite_gb'    : max_gb,
            'percentual'   : min(percentual, 100),
            'ffmpeg_ok'    : _FFMPEG_OK
        })

    except Exception as e:
        logger.error(f"Erro ao obter estatísticas: {e}")
        return jsonify({'erro': str(e)}), 500


# ── Limpeza Automática ─────────────────────────────────────────────────────
def calcular_espaco_usado(gravacoes_path: str) -> int:
    total = 0
    if os.path.exists(gravacoes_path):
        for f in os.listdir(gravacoes_path):
            fp = os.path.join(gravacoes_path, f)
            if os.path.isfile(fp):
                total += os.path.getsize(fp)
    return total


def limpar_videos_antigos(db_path: str,
                           gravacoes_path: str,
                           thumbs_path: str):
    try:
        with get_conexao(db_path) as conn:
            max_gb = float(conn.execute(
                "SELECT valor FROM configuracoes "
                "WHERE chave = 'max_armazenamento_gb'"
            ).fetchone()['valor'])

            max_bytes = int(max_gb * 1024 * 1024 * 1024)
            usado     = calcular_espaco_usado(gravacoes_path)

            if usado <= max_bytes:
                return

            logger.warning(
                f"Armazenamento cheio: "
                f"{usado/1024/1024/1024:.2f}GB / {max_gb}GB"
            )

            rows = conn.execute(
                "SELECT * FROM eventos ORDER BY data_evento ASC"
            ).fetchall()

            removidos = 0
            for row in rows:
                if usado <= max_bytes * 0.9:
                    break

                video_path = os.path.join(gravacoes_path, row['arquivo_video'])
                thumb_path = os.path.join(thumbs_path,    row['arquivo_thumb'])

                if os.path.exists(video_path):
                    tamanho = os.path.getsize(video_path)
                    os.remove(video_path)
                    usado    -= tamanho
                    removidos += 1

                if os.path.exists(thumb_path):
                    os.remove(thumb_path)

                conn.execute(
                    "DELETE FROM eventos WHERE id = ?", (row['id'],)
                )

            conn.commit()
            logger.info(
                f"Limpeza automática: {removidos} vídeo(s) removido(s)"
            )

    except Exception as e:
        logger.error(f"Erro na limpeza automática: {e}")


def iniciar_limpeza_automatica(db_path: str, gravacoes_path: str):
    thumbs_path = os.path.join(os.path.dirname(gravacoes_path), 'thumbs')
    while True:
        time.sleep(600)
        try:
            limpar_videos_antigos(db_path, gravacoes_path, thumbs_path)
        except Exception as e:
            logger.error(f"Erro no loop de limpeza: {e}")
