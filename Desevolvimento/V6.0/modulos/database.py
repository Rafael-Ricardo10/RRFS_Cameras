#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Módulo de Banco de Dados
Inclui: Usuários · Câmeras · Eventos · Configurações
        + Pessoas Cadastradas (Facial) · Rostos Detectados (Galeria)
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
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        nome                    TEXT    NOT NULL,
        ip                      TEXT    NOT NULL,
        porta                   INTEGER NOT NULL DEFAULT 554,
        usuario                 TEXT    NOT NULL DEFAULT '',
        senha                   TEXT    NOT NULL DEFAULT '',
        protocolo               TEXT    NOT NULL DEFAULT 'rtsp',
        rtsp_path               TEXT    NOT NULL DEFAULT '',
        transporte              TEXT    NOT NULL DEFAULT 'auto',
        ativa                   INTEGER NOT NULL DEFAULT 1,
        gravar                  INTEGER NOT NULL DEFAULT 0,
        sensibilidade           INTEGER NOT NULL DEFAULT 50,
        zona_pontos             TEXT    NOT NULL DEFAULT '',

        -- ── Inteligência Artificial ─────────────────────────────
        ia_ativa                INTEGER NOT NULL DEFAULT 0,
        detectar_pessoa         INTEGER NOT NULL DEFAULT 1,
        detectar_veiculo        INTEGER NOT NULL DEFAULT 0,
        detectar_animal         INTEGER NOT NULL DEFAULT 0,
        reconhecimento_facial   INTEGER NOT NULL DEFAULT 0,
        confianca_minima        REAL    NOT NULL DEFAULT 0.5,
        modo_gravacao           TEXT    NOT NULL DEFAULT 'movimento',
        pessoa_alvo_id          INTEGER DEFAULT NULL,

        criado_em               TEXT    DEFAULT (datetime('now', 'localtime'))
    );

    CREATE TABLE IF NOT EXISTS eventos (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        camera_id           INTEGER NOT NULL,
        arquivo_video       TEXT    NOT NULL,
        arquivo_thumb       TEXT    NOT NULL,
        duracao             INTEGER NOT NULL DEFAULT 0,
        tamanho_bytes       INTEGER NOT NULL DEFAULT 0,

        -- ── Metadados de IA ──────────────────────────────────────
        objetos_detectados  TEXT    NOT NULL DEFAULT '',
        pessoa_reconhecida  TEXT    NOT NULL DEFAULT '',
        confianca_ia        REAL    NOT NULL DEFAULT 0,

        data_evento         TEXT    DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (camera_id) REFERENCES cameras(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS configuracoes (
        chave   TEXT PRIMARY KEY,
        valor   TEXT NOT NULL
    );

    -- ── Pessoas Cadastradas (Reconhecimento Facial) ─────────────────
    CREATE TABLE IF NOT EXISTS pessoas_cadastradas (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        nome            TEXT    NOT NULL,
        foto_path       TEXT    NOT NULL,
        face_encoding   BLOB    NOT NULL,
        ativo           INTEGER NOT NULL DEFAULT 1,
        criado_em       TEXT    DEFAULT (datetime('now', 'localtime'))
    );

    -- ── Galeria de Rostos Detectados pelas Câmeras ──────────────────
    CREATE TABLE IF NOT EXISTS rostos_detectados (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        camera_id           INTEGER NOT NULL,
        evento_id           INTEGER DEFAULT NULL,
        pessoa_id           INTEGER DEFAULT NULL,
        nome_identificado   TEXT    NOT NULL DEFAULT 'Desconhecido',
        confianca           REAL    NOT NULL DEFAULT 0,
        foto_rosto          TEXT    NOT NULL,
        data_deteccao       TEXT    DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (camera_id) REFERENCES cameras(id)  ON DELETE CASCADE,
        FOREIGN KEY (evento_id) REFERENCES eventos(id)  ON DELETE SET NULL,
        FOREIGN KEY (pessoa_id) REFERENCES pessoas_cadastradas(id) ON DELETE SET NULL
    );

    INSERT OR IGNORE INTO configuracoes (chave, valor)
        VALUES ('max_armazenamento_gb', '10');

    CREATE INDEX IF NOT EXISTS idx_eventos_camera
        ON eventos(camera_id);
    CREATE INDEX IF NOT EXISTS idx_eventos_data
        ON eventos(data_evento DESC);
    CREATE INDEX IF NOT EXISTS idx_rostos_camera
        ON rostos_detectados(camera_id);
    CREATE INDEX IF NOT EXISTS idx_rostos_data
        ON rostos_detectados(data_deteccao DESC);
    CREATE INDEX IF NOT EXISTS idx_rostos_pessoa
        ON rostos_detectados(pessoa_id);
    """

    with get_conexao(db_path) as conn:
        conn.executescript(schema)

        # ── Migrações seguras (bancos já existentes) ────────────────────
        migracoes = [
            "ALTER TABLE cameras ADD COLUMN zona_pontos TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE cameras ADD COLUMN rtsp_path   TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE cameras ADD COLUMN transporte  TEXT NOT NULL DEFAULT 'auto'",

            # Colunas de IA
            "ALTER TABLE cameras ADD COLUMN ia_ativa              INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE cameras ADD COLUMN detectar_pessoa       INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE cameras ADD COLUMN detectar_veiculo      INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE cameras ADD COLUMN detectar_animal       INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE cameras ADD COLUMN reconhecimento_facial INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE cameras ADD COLUMN confianca_minima      REAL    NOT NULL DEFAULT 0.5",
            "ALTER TABLE cameras ADD COLUMN modo_gravacao         TEXT    NOT NULL DEFAULT 'movimento'",
            "ALTER TABLE cameras ADD COLUMN pessoa_alvo_id        INTEGER DEFAULT NULL",

            # Colunas de IA em eventos
            "ALTER TABLE eventos ADD COLUMN objetos_detectados TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE eventos ADD COLUMN pessoa_reconhecida TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE eventos ADD COLUMN confianca_ia       REAL NOT NULL DEFAULT 0",
        ]
        for sql in migracoes:
            try:
                conn.execute(sql)
                logger.info(f"Migração aplicada: {sql}")
            except Exception:
                pass  # Coluna já existe

    logger.info("Banco de dados inicializado com sucesso.")
