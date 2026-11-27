#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Módulo de Banco de Dados
"""

import sqlite3
import logging
from contextlib import contextmanager

logger = logging.getLogger('RRFS.database')


@contextmanager
def get_conexao(db_path: str):
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
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
    logger.info(f"Inicializando banco: {db_path}")

    schema = """
    CREATE TABLE IF NOT EXISTS usuarios (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario     TEXT    NOT NULL UNIQUE,
        senha_hash  TEXT    NOT NULL,
        criado_em   TEXT    DEFAULT (datetime('now', 'localtime'))
    );

    CREATE TABLE IF NOT EXISTS cameras (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        nome          TEXT    NOT NULL,
        ip            TEXT    NOT NULL,
        porta         INTEGER NOT NULL DEFAULT 554,
        usuario       TEXT    NOT NULL DEFAULT '',
        senha         TEXT    NOT NULL DEFAULT '',
        protocolo     TEXT    NOT NULL DEFAULT 'rtsp',
        ativa         INTEGER NOT NULL DEFAULT 1,
        gravar        INTEGER NOT NULL DEFAULT 0,
        sensibilidade INTEGER NOT NULL DEFAULT 50,
        zona_pontos   TEXT    NOT NULL DEFAULT '',
        criado_em     TEXT    DEFAULT (datetime('now', 'localtime'))
    );

    CREATE TABLE IF NOT EXISTS eventos (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        camera_id       INTEGER NOT NULL,
        arquivo_video   TEXT    NOT NULL,
        arquivo_thumb   TEXT    NOT NULL,
        duracao         INTEGER NOT NULL DEFAULT 0,
        tamanho_bytes   INTEGER NOT NULL DEFAULT 0,
        data_evento     TEXT    DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (camera_id) REFERENCES cameras(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS configuracoes (
        chave   TEXT PRIMARY KEY,
        valor   TEXT NOT NULL
    );

    INSERT OR IGNORE INTO configuracoes (chave, valor)
        VALUES ('max_armazenamento_gb', '10');

    CREATE INDEX IF NOT EXISTS idx_eventos_camera
        ON eventos(camera_id);
    CREATE INDEX IF NOT EXISTS idx_eventos_data
        ON eventos(data_evento DESC);
    """

    with get_conexao(db_path) as conn:
        conn.executescript(schema)

        # Migração: adiciona zona_pontos se não existir
        try:
            conn.execute("ALTER TABLE cameras ADD COLUMN zona_pontos TEXT NOT NULL DEFAULT ''")
            logger.info("Coluna zona_pontos adicionada.")
        except Exception:
            pass  # Já existe

    logger.info("Banco de dados inicializado com sucesso.")
