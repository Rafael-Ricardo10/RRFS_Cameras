#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Módulo de Autenticação
"""

import logging
import hmac
import time
from flask import (Blueprint, render_template, request,
                   redirect, url_for, flash, current_app)
from flask_login import (LoginManager, UserMixin,
                         login_user, logout_user,
                         login_required, current_user)
import hashlib
from werkzeug.security import generate_password_hash, check_password_hash

from modulos.database import get_conexao

logger = logging.getLogger('RRFS.auth')
auth_bp = Blueprint('auth', __name__)


# ── Modelo de Usuário ──────────────────────────────────────────────────────
class Usuario(UserMixin):
    def __init__(self, id_: int, nome: str):
        self.id = id_
        self.nome = nome

    def get_id(self):
        return str(self.id)


# ── Helpers ────────────────────────────────────────────────────────────────
def hash_senha(senha: str) -> str:
    """Gera hash SHA-256 com salt fixo do sistema."""
    salt = "RRFS_CAM_2024"
    return hashlib.sha256(f"{salt}{senha}".encode()).hexdigest()


def gerar_hash_senha(senha: str) -> str:
    """Hash forte (scrypt/pbkdf2 com salt aleatório por usuário)."""
    return generate_password_hash(senha)


def verificar_senha(senha: str, armazenado: str) -> bool:
    """Aceita hash novo e o formato antigo (SHA-256) para não trancar
    usuários existentes; o formato antigo é atualizado no login."""
    if armazenado.startswith(('pbkdf2:', 'scrypt:')):
        return check_password_hash(armazenado, senha)
    return hmac.compare_digest(armazenado, hash_senha(senha))


# ── Limite de tentativas de login (anti força-bruta) ─────────────────────
_LOGIN_MAX_FALHAS = 5
_LOGIN_JANELA_S   = 300
_login_falhas: dict = {}


def _login_bloqueado(chave: str) -> bool:
    agora = time.time()
    tentativas = [t for t in _login_falhas.get(chave, [])
                  if agora - t < _LOGIN_JANELA_S]
    _login_falhas[chave] = tentativas
    return len(tentativas) >= _LOGIN_MAX_FALHAS


def _login_registrar_falha(chave: str):
    _login_falhas.setdefault(chave, []).append(time.time())


def usuario_existe(db_path: str) -> bool:
    """Verifica se já existe algum usuário cadastrado."""
    try:
        with get_conexao(db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) as total FROM usuarios"
            ).fetchone()
            return row['total'] > 0
    except Exception:
        return False


def buscar_usuario_por_id(db_path: str, user_id: int):
    """Retorna objeto Usuario ou None."""
    try:
        with get_conexao(db_path) as conn:
            row = conn.execute(
                "SELECT id, usuario FROM usuarios WHERE id = ?",
                (user_id,)
            ).fetchone()
            if row:
                return Usuario(row['id'], row['usuario'])
    except Exception as e:
        logger.error(f"Erro ao buscar usuário: {e}")
    return None


# ── Login Manager ──────────────────────────────────────────────────────────
def login_manager_init(login_manager: LoginManager, app):
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Por favor, faça login para continuar.'
    login_manager.login_message_category = 'warning'
    login_manager.init_app(app)

    @login_manager.user_loader
    def carregar_usuario(user_id):
        return buscar_usuario_por_id(app.config['DB_PATH'], int(user_id))


# ── Rotas ──────────────────────────────────────────────────────────────────
@auth_bp.route('/setup', methods=['GET', 'POST'])
def setup():
    """Primeiro acesso - cria usuário administrador."""
    db = current_app.config['DB_PATH']

    if usuario_existe(db):
        return redirect(url_for('auth.login'))

    erro = None
    if request.method == 'POST':
        usuario = request.form.get('usuario', '').strip()
        senha = request.form.get('senha', '').strip()
        confirma = request.form.get('confirma', '').strip()

        if not usuario or len(usuario) < 3:
            erro = 'Usuário deve ter pelo menos 3 caracteres.'
        elif not senha or len(senha) < 6:
            erro = 'Senha deve ter pelo menos 6 caracteres.'
        elif senha != confirma:
            erro = 'As senhas não conferem.'
        else:
            try:
                with get_conexao(db) as conn:
                    conn.execute(
                        "INSERT INTO usuarios (usuario, senha_hash) VALUES (?, ?)",
                        (usuario, gerar_hash_senha(senha))
                    )
                logger.info(f"Usuário '{usuario}' criado com sucesso.")
                flash('Usuário criado! Faça o login.', 'success')
                return redirect(url_for('auth.login'))
            except Exception as e:
                erro = f'Erro ao criar usuário: {e}'
                logger.error(erro)

    return render_template('setup.html', erro=erro)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Página de login."""
    db = current_app.config['DB_PATH']

    if not usuario_existe(db):
        return redirect(url_for('auth.setup'))

    if current_user.is_authenticated:
        return redirect(url_for('cameras.dashboard'))

    erro = None
    if request.method == 'POST':
        usuario = request.form.get('usuario', '').strip()
        senha = request.form.get('senha', '').strip()

        chave_rl = f"{request.remote_addr}|{usuario.lower()}"
        if _login_bloqueado(chave_rl):
            logger.warning(f"Login bloqueado (muitas falhas): '{usuario}'")
            return render_template(
                'login.html',
                erro='Muitas tentativas. Aguarde 5 minutos e tente novamente.'
            )

        try:
            with get_conexao(db) as conn:
                row = conn.execute(
                    "SELECT id, usuario, senha_hash FROM usuarios WHERE usuario = ?",
                    (usuario,)
                ).fetchone()
                if row and verificar_senha(senha, row['senha_hash']):
                    if not row['senha_hash'].startswith(('pbkdf2:', 'scrypt:')):
                        conn.execute(
                            "UPDATE usuarios SET senha_hash = ? WHERE id = ?",
                            (gerar_hash_senha(senha), row['id'])
                        )
                else:
                    row = None

            if row:
                _login_falhas.pop(chave_rl, None)
                u = Usuario(row['id'], row['usuario'])
                login_user(u, remember=False)
                logger.info(f"Login: '{usuario}'")
                return redirect(url_for('cameras.dashboard'))
            else:
                erro = 'Usuário ou senha incorretos.'
                _login_registrar_falha(chave_rl)
                logger.warning(f"Tentativa de login inválida: '{usuario}'")
        except Exception as e:
            erro = f'Erro interno: {e}'
            logger.error(erro)

    return render_template('login.html', erro=erro)


@auth_bp.route('/logout')
@login_required
def logout():
    logger.info(f"Logout: '{current_user.nome}'")
    logout_user()
    return redirect(url_for('auth.login'))
