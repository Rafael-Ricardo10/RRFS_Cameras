#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Módulo de Banco de Dados
Gerencia SQLite com controle de conexões e sem vazamento de memória
"""

import sqlite3
import logging
import os
from contextlib import contextmanager

logger = logging.getLogger('RRFS.database')


@contextmanager
def get_conexao(db_path: str):
    """
    Context manager para conexão SQLite.
    Garante fechamento correto e evita vazamento de recursos.
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")   # melhor concorrência
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        conn.commit()
    except sqlite3.Error as e:
        if conn:
            conn.rollback()
        logger.error(f"Erro no banco de dados: {e}")
        raise
    finally:
        if conn:
            conn.close()


def iniciar_banco(db_path: str) -> None:
    """Cria tabelas iniciais se não existirem."""
    logger.info(f"Inicializando banco: {db_path}")

    schema = """
    CREATE TABLE IF NOT EXISTS usuarios (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario     TEXT    NOT NULL UNIQUE,
        senha_hash  TEXT    NOT NULL,
        criado_em   TEXT    DEFAULT (datetime('now', 'localtime'))
    );

    CREATE TABLE IF NOT EXISTS cameras (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        nome        TEXT    NOT NULL,
        ip          TEXT    NOT NULL,
        porta       INTEGER NOT NULL DEFAULT 554,
        usuario     TEXT    NOT NULL DEFAULT '',
        senha       TEXT    NOT NULL DEFAULT '',
        protocolo   TEXT    NOT NULL DEFAULT 'rtsp',
        ativa       INTEGER NOT NULL DEFAULT 1,
        criado_em   TEXT    DEFAULT (datetime('now', 'localtime'))
    );

    CREATE TABLE IF NOT EXISTS logs_sistema (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        nivel       TEXT    NOT NULL,
        mensagem    TEXT    NOT NULL,
        criado_em   TEXT    DEFAULT (datetime('now', 'localtime'))
    );
    """

    with get_conexao(db_path) as conn:
        conn.executescript(schema)

    logger.info("Banco de dados inicializado com sucesso.")
