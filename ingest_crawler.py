"""
ingest_crawler.py — Ingestão de participantes no DB de benchmarks com a ROTA inferida.

Fonte ÚNICA de verdade usada por crawler.py, crawlerHighElo.py e corrigir_posicoes.py.
Em vez de gravar o `teamPosition` cru, cruza os sinais da match-v5 (ver deteccao_rota.py) e:
  - grava a ROTA inferida em `posicao` (final);
  - PULA o participante quando a rota não é confiável (descartado);
  - persiste os SINAIS CRUS (team_position, individual_position, lane, role, smites, puuid,
    team_id) para que mudanças FUTURAS na lógica re-derivem do DB, SEM re-buscar a API.
"""

from deteccao_rota import inferir_rotas_partida

# Colunas adicionadas para guardar os sinais crus (idempotente via garantir_colunas).
COLUNAS_NOVAS = [
    ("puuid", "TEXT"),
    ("team_id", "INTEGER"),
    ("team_position", "TEXT"),          # rótulo CRU da Riot (deduplicado) — antes ia em 'posicao'
    ("individual_position", "TEXT"),
    ("lane", "TEXT"),
    ("role", "TEXT"),
    ("summoner1_id", "INTEGER"),
    ("summoner2_id", "INTEGER"),
    ("posicao_apoio", "INTEGER"),       # nº de sinais que concordaram com a rota inferida
]

# 31 colunas originais + 9 novas = 40
_COLUNAS_INSERT = (
    "match_id, regiao, elo, divisao, campeao, posicao, vitoria, "
    "kda, cs_min, ouro_min, visao_min, dano_min, itens, "
    "dano_objetivos, dano_torres, tempo_cc, pink_wards, "
    "cura_total, dano_mitigado, tempo_vivo, first_blood, fb_assist, "
    "pings_perigo, pings_ajuda, pings_mia, "
    "kpa, skillshots_desviadas, solo_kills, cs_jungle_10m, cs_rota_10m, pct_dano_time, "
    "puuid, team_id, team_position, individual_position, lane, role, summoner1_id, summoner2_id, posicao_apoio"
)
_SQL_INSERT = (
    f"INSERT INTO estatisticas_meta ({_COLUNAS_INSERT}) VALUES ("
    + ",".join(["?"] * 40) + ")"
)


def garantir_colunas(conn):
    """Adiciona as colunas de sinais crus se ainda não existirem (idempotente)."""
    existentes = {r[1] for r in conn.execute("PRAGMA table_info(estatisticas_meta)")}
    for nome, tipo in COLUNAS_NOVAS:
        if nome not in existentes:
            conn.execute(f"ALTER TABLE estatisticas_meta ADD COLUMN {nome} {tipo}")
    conn.commit()


def linha_participante(p: dict, m_id, servidor, elo, div, posicao, apoio, duracao_min) -> tuple:
    """Constrói a tupla de 40 valores de UM participante (ordem de _SQL_INSERT)."""
    kda = (p["kills"] + p["assists"]) / max(1, p["deaths"])
    cs = p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0)
    dano = p.get("totalDamageDealtToChampions", 0)
    itens_str = ",".join([str(p.get(f"item{i}", 0)) for i in range(7)])
    chal = p.get("challenges", {}) or {}
    return (
        m_id, servidor, elo, div, p.get("championName"), posicao, 1 if p.get("win") else 0,
        # Base
        kda, cs / duracao_min, p.get("goldEarned", 0) / duracao_min,
        p.get("visionScore", 0) / duracao_min, dano / duracao_min, itens_str,
        # Macro
        p.get("damageDealtToObjectives", 0), p.get("damageDealtToBuildings", 0),
        p.get("timeCCingOthers", 0), p.get("visionWardsBoughtInGame", 0),
        # Micro
        p.get("totalHeal", 0), p.get("damageSelfMitigated", 0), p.get("longestTimeSpentLiving", 0),
        1 if p.get("firstBloodKill") else 0, 1 if p.get("firstBloodAssist") else 0,
        # Pings
        (p.get("getBackPings", 0) + p.get("dangerPings", 0)),
        p.get("assistMePings", 0), p.get("enemyMissingPings", 0),
        # Challenges
        chal.get("killParticipation", 0), chal.get("skillshotsDodged", 0), chal.get("soloKills", 0),
        chal.get("jungleCsBefore10Minutes", 0), chal.get("laneMinionsFirst10Minutes", 0),
        chal.get("teamDamagePercentage", 0),
        # Sinais crus (novas colunas) — para re-derivação futura sem re-fetch
        p.get("puuid"), p.get("teamId"), p.get("teamPosition"), p.get("individualPosition"),
        p.get("lane"), p.get("role"), p.get("summoner1Id"), p.get("summoner2Id"), apoio,
    )


def inserir_partida(cursor, data: dict, m_id, servidor, elo, div) -> tuple[int, int]:
    """Insere os participantes de UMA partida com a rota inferida.
    Retorna (inseridos, descartados). NÃO faz commit nem marca partidas_processadas —
    quem chama controla isso (mantém o fluxo atual do crawler)."""
    info = data["info"]
    duracao_min = info["gameDuration"] / 60.0
    rotas = inferir_rotas_partida(info)
    inseridos = descartados = 0
    for p in info["participants"]:
        inf = rotas.get(p.get("puuid"), {})
        if not inf.get("confiavel"):
            descartados += 1
            continue  # rota indeduzível (swap/autofill ambíguo) → fora do dataset
        cursor.execute(
            _SQL_INSERT,
            linha_participante(p, m_id, servidor, elo, div, inf["rota"], inf.get("apoio"), duracao_min),
        )
        inseridos += 1
    return inseridos, descartados
