#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RRFS Cameras - Módulo de Banco de Dados
Inclui: Usuários · Câmeras · Eventos · Configurações
        + Pessoas Cadastradas (Facial) · Rostos Detectados (Galeria)
        + Controle PTZ (ONVIF) · Presets · Patrulha · Auto-Tracking

CORREÇÃO (lentidão/erro de PTZ): as colunas onvif_tipo, onvif_caminho,
onvif_parada, ptz_tracking e ptz_tracking_zona estavam sendo usadas pelo
módulo ptz_controle.py mas nunca existiam no schema nem nas migrações —
isso fazia TODO salvamento de configuração PTZ falhar silenciosamente
("no such column: onvif_tipo"), fazendo a tela sempre reverter para os
valores padrão.

NOVO (v6): colunas tipo/pan/tilt/zoom na tabela ptz_presets — permitem
salvar uma "posição PTZ" mesmo em câmeras que não suportam o recurso
opcional SetPreset/GotoPreset do ONVIF (erro comum: "Optional Action
Not Implemented"). Nesse caso, a posição é guardada como coordenadas
absolutas (pan/tilt/zoom) direto no nosso banco, e o retorno a ela é
feito via AbsoluteMove em vez de GotoPreset. Ver ptz_controle.py.

NOVO (v8): coluna ptz_patrulha_continua — permite que a patrulha
percorra os presets de forma CONTÍNUA (sem parar o tempo configurado
em cada posição), controlada apenas pela velocidade escolhida pelo
usuário. Ver ptz_controle.py (PatrulhaPTZ._loop).
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

        -- ── Controle PTZ (ONVIF) ─────────────────────────────────
        ptz_ativo               INTEGER NOT NULL DEFAULT 0,
        onvif_porta             INTEGER NOT NULL DEFAULT 80,
        onvif_usuario           TEXT    NOT NULL DEFAULT '',
        onvif_senha             TEXT    NOT NULL DEFAULT '',
        onvif_tipo              TEXT    NOT NULL DEFAULT 'netcat_onvif',
        onvif_caminho           TEXT    NOT NULL DEFAULT '/onvif/deviceio_service',
        onvif_parada            TEXT    NOT NULL DEFAULT 'auto',
        ptz_calibrado           INTEGER NOT NULL DEFAULT 0,
        ptz_invert_pan          INTEGER NOT NULL DEFAULT 0,
        ptz_invert_tilt         INTEGER NOT NULL DEFAULT 0,
        ptz_pan_scale           REAL    NOT NULL DEFAULT 1.0,
        ptz_tilt_scale          REAL    NOT NULL DEFAULT 1.0,
        modo_ptz                TEXT    NOT NULL DEFAULT 'manual',
        ptz_velocidade_manual   REAL    NOT NULL DEFAULT 0.5,
        ptz_velocidade_patrulha REAL    NOT NULL DEFAULT 0.5,
        ptz_patrulha_ativa      INTEGER NOT NULL DEFAULT 0,
        ptz_patrulha_continua   INTEGER NOT NULL DEFAULT 0,
        ptz_tracking            INTEGER NOT NULL DEFAULT 0,
        ptz_tracking_zona       INTEGER NOT NULL DEFAULT 20,

        criado_em               TEXT    DEFAULT (datetime('now', 'localtime'))
    );

    CREATE TABLE IF NOT EXISTS eventos (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        camera_id           INTEGER NOT NULL,
        arquivo_video       TEXT    NOT NULL,
        arquivo_thumb       TEXT    NOT NULL,
        duracao             INTEGER NOT NULL DEFAULT 0,
        tamanho_bytes       INTEGER NOT NULL DEFAULT 0,

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

    -- ── Presets de Posição PTZ ───────────────────────────────────────
    -- tipo='camera' → preset nativo ONVIF (SetPreset/GotoPreset na câmera)
    -- tipo='local'  → fallback: pan/tilt/zoom absolutos guardados aqui
    --                 (usado quando a câmera não suporta SetPreset —
    --                 erro ONVIF "Optional Action Not Implemented")
    CREATE TABLE IF NOT EXISTS ptz_presets (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        camera_id       INTEGER NOT NULL,
        nome            TEXT    NOT NULL,
        token           TEXT    NOT NULL,
        ordem           INTEGER NOT NULL DEFAULT 0,
        tempo_parada    INTEGER NOT NULL DEFAULT 8,
        tipo            TEXT    NOT NULL DEFAULT 'camera',
        pan             REAL    DEFAULT NULL,
        tilt            REAL    DEFAULT NULL,
        zoom            REAL    DEFAULT NULL,
        criado_em       TEXT    DEFAULT (datetime('now', 'localtime')),
        FOREIGN KEY (camera_id) REFERENCES cameras(id) ON DELETE CASCADE
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
    CREATE INDEX IF NOT EXISTS idx_presets_camera
        ON ptz_presets(camera_id);
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

            # Colunas de PTZ
            "ALTER TABLE cameras ADD COLUMN ptz_ativo               INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE cameras ADD COLUMN onvif_porta             INTEGER NOT NULL DEFAULT 80",
            "ALTER TABLE cameras ADD COLUMN onvif_usuario           TEXT    NOT NULL DEFAULT ''",
            "ALTER TABLE cameras ADD COLUMN onvif_senha             TEXT    NOT NULL DEFAULT ''",
            # ── CORREÇÃO: colunas que faltavam (causavam "no such column") ──
            "ALTER TABLE cameras ADD COLUMN onvif_tipo              TEXT    NOT NULL DEFAULT 'netcat_onvif'",
            "ALTER TABLE cameras ADD COLUMN onvif_caminho           TEXT    NOT NULL DEFAULT '/onvif/deviceio_service'",
            "ALTER TABLE cameras ADD COLUMN onvif_parada            TEXT    NOT NULL DEFAULT 'auto'",
            "ALTER TABLE cameras ADD COLUMN ptz_calibrado           INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE cameras ADD COLUMN ptz_invert_pan          INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE cameras ADD COLUMN ptz_invert_tilt         INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE cameras ADD COLUMN ptz_pan_scale           REAL    NOT NULL DEFAULT 1.0",
            "ALTER TABLE cameras ADD COLUMN ptz_tilt_scale          REAL    NOT NULL DEFAULT 1.0",
            "ALTER TABLE cameras ADD COLUMN modo_ptz                TEXT    NOT NULL DEFAULT 'manual'",
            "ALTER TABLE cameras ADD COLUMN ptz_velocidade_manual   REAL    NOT NULL DEFAULT 0.5",
            "ALTER TABLE cameras ADD COLUMN ptz_velocidade_patrulha REAL    NOT NULL DEFAULT 0.5",
            "ALTER TABLE cameras ADD COLUMN ptz_patrulha_ativa      INTEGER NOT NULL DEFAULT 0",
            # ── NOVO (v8): patrulha contínua (sem paradas nos presets) ──
            "ALTER TABLE cameras ADD COLUMN ptz_patrulha_continua   INTEGER NOT NULL DEFAULT 0",
            # ── CORREÇÃO: colunas de tracking (antes só existiam via
            #    patch em runtime no ptz_controle.py) ─────────────────
            "ALTER TABLE cameras ADD COLUMN ptz_tracking            INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE cameras ADD COLUMN ptz_tracking_zona       INTEGER NOT NULL DEFAULT 20",

            # ── NOVO (v6): fallback de preset local (pan/tilt/zoom) ──
            # para câmeras sem suporte a SetPreset/GotoPreset ONVIF.
            "ALTER TABLE ptz_presets ADD COLUMN tipo TEXT NOT NULL DEFAULT 'camera'",
            "ALTER TABLE ptz_presets ADD COLUMN pan  REAL DEFAULT NULL",
            "ALTER TABLE ptz_presets ADD COLUMN tilt REAL DEFAULT NULL",
            "ALTER TABLE ptz_presets ADD COLUMN zoom REAL DEFAULT NULL",
        ]
        for sql in migracoes:
            try:
                conn.execute(sql)
                logger.info(f"Migração aplicada: {sql}")
            except Exception:
                pass  # Coluna já existe

    logger.info("Banco de dados inicializado com sucesso.")
