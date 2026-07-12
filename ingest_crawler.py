"""
ingest_crawler.py — Ingestão de participantes no DB de benchmarks com a ROTA inferida.

Fonte ÚNICA de verdade usada por crawler.py e crawlerHighElo.py.
Em vez de gravar o `teamPosition` cru, cruza os sinais da match-v5 (ver deteccao_rota.py) e:
  - grava a ROTA inferida em `posicao` (final);
  - PULA o participante quando a rota não é confiável (descartado);
  - persiste os SINAIS CRUS (team_position, individual_position, lane, role, smites, puuid,
    team_id) para que mudanças FUTURAS na lógica re-derivem do DB, SEM re-buscar a API.
"""

import json
import os
import requests

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
    # fila DEFAULT 'solo': os 4,3M de registros pré-existentes são TODOS queue 420 (solo).
    # O DEFAULT faz o SQLite tratá-los como 'solo' sem reescrever a tabela — sem ele, as
    # agregações (que filtram fila='solo') perderiam todo o histórico. Ver as queries do app.
    ("fila", "TEXT DEFAULT 'solo'"),    # 'solo' | 'flex' | 'normal'
    # botas_compradas: IDs de bota comprados na timeline, em ordem. Corrige o top_2_botas
    # (o inventário final quase nunca tem bota — vendida no late; ver a investigação de 08/07).
    ("botas_compradas", "TEXT"),        # NULL para dados antigos (sem timeline)
    # Guia do Campeão (Fase 1): saem de graça do que o crawler já baixa — runas do
    # participant.perks (match) e ordem de skill dos SKILL_LEVEL_UP (timeline já buscada
    # para as botas). NULL para dados antigos (degradação segura, como botas_compradas).
    ("runas", "TEXT"),                  # JSON: estilos + seleções + shards
    ("ordem_skill", "TEXT"),            # sequência de skills (ex.: 'QWEQ...')
]

# 31 colunas originais + 13 novas = 44
_COLUNAS_INSERT = (
    "match_id, regiao, elo, divisao, campeao, posicao, vitoria, "
    "kda, cs_min, ouro_min, visao_min, dano_min, itens, "
    "dano_objetivos, dano_torres, tempo_cc, pink_wards, "
    "cura_total, dano_mitigado, tempo_vivo, first_blood, fb_assist, "
    "pings_perigo, pings_ajuda, pings_mia, "
    "kpa, skillshots_desviadas, solo_kills, cs_jungle_10m, cs_rota_10m, pct_dano_time, "
    "puuid, team_id, team_position, individual_position, lane, role, summoner1_id, summoner2_id, posicao_apoio, "
    "fila, botas_compradas, runas, ordem_skill"
)
_SQL_INSERT = (
    f"INSERT INTO estatisticas_meta ({_COLUNAS_INSERT}) VALUES ("
    + ",".join(["?"] * 44) + ")"
)

# ---------------------------------------------------------------------------
# Botas compradas (da timeline) — mapa de IDs de bota, carregado uma vez.
# ---------------------------------------------------------------------------
_BOTAS_IDS = None


def _ids_de_botas() -> set:
    """Conjunto de IDs de TODOS os itens com tag Boots (inclui a Bota básica 1001).
    Carregado uma vez do DDragon, com backup local — mesmo padrão do atualizador."""
    global _BOTAS_IDS
    if _BOTAS_IDS is not None:
        return _BOTAS_IDS
    backup = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup_botas_ids.json")
    try:
        v = requests.get("https://ddragon.leagueoflegends.com/api/versions.json", timeout=5).json()[0]
        itens = requests.get(
            f"https://ddragon.leagueoflegends.com/cdn/{v}/data/pt_BR/item.json", timeout=10
        ).json()["data"]
        _BOTAS_IDS = {i for i, d in itens.items() if "Boots" in d.get("tags", [])}
        with open(backup, "w") as f:
            json.dump(sorted(_BOTAS_IDS), f)
    except Exception:
        if os.path.exists(backup):
            with open(backup) as f:
                _BOTAS_IDS = set(json.load(f))
        else:
            _BOTAS_IDS = set()  # sem botas conhecidas → botas_compradas fica NULL (degradação segura)
    return _BOTAS_IDS


def _botas_por_participante(timeline: dict) -> dict:
    """participantId (1-10) -> lista de IDs de bota comprados, em ordem de compra.
    Lê os eventos ITEM_PURCHASED da timeline da Match-V5; ignora itens não-bota."""
    ids_botas = _ids_de_botas()
    out: dict = {}
    for frame in (timeline or {}).get("info", {}).get("frames", []):
        for ev in frame.get("events", []):
            if ev.get("type") == "ITEM_PURCHASED":
                iid = str(ev.get("itemId"))
                if iid in ids_botas:
                    out.setdefault(ev.get("participantId"), []).append(iid)
    return out


# ---------------------------------------------------------------------------
# Guia do Campeão (Fase 1): runas (do match) e ordem de skill (da timeline).
# ---------------------------------------------------------------------------
# Slot da timeline -> letra da skill. Não há um slot "P" (passiva não sobe nível).
_SLOT_SKILL = {1: "Q", 2: "W", 3: "E", 4: "R"}


def _runas_de_participante(p: dict) -> str | None:
    """Serializa `p['perks']` (Match-V5) num JSON compacto com estilo primário/secundário,
    as seleções (IDs das runas) de cada árvore e os 3 stat shards. NULL se ausente
    (dados antigos ou partida sem perks) — degradação segura como botas_compradas."""
    perks = p.get("perks") or {}
    styles = perks.get("styles") or []
    primaria = next((s for s in styles if s.get("description") == "primaryStyle"), None)
    secundaria = next((s for s in styles if s.get("description") == "subStyle"), None)
    if not primaria or not secundaria:
        return None
    stats = perks.get("statPerks") or {}
    dados = {
        "estilo_primario": primaria.get("style"),
        "estilo_secundario": secundaria.get("style"),
        "primarias": [sel.get("perk") for sel in primaria.get("selections", [])],
        "secundarias": [sel.get("perk") for sel in secundaria.get("selections", [])],
        "shards": [stats.get("offense"), stats.get("flex"), stats.get("defense")],
    }
    return json.dumps(dados, separators=(",", ":"))


def _ordem_skill(timeline: dict) -> dict:
    """participantId (1-10) -> string da sequência de skills na ordem em que foram subidas
    (ex.: 'QWEQQRQ...'). Lê os SKILL_LEVEL_UP da timeline da Match-V5. Ignora EVOLVE
    (Kha'Zix/Viktor evoluem sem gastar ponto de nível), contando só os NORMAL."""
    out: dict = {}
    for frame in (timeline or {}).get("info", {}).get("frames", []):
        for ev in frame.get("events", []):
            if ev.get("type") == "SKILL_LEVEL_UP" and ev.get("levelUpType") == "NORMAL":
                letra = _SLOT_SKILL.get(ev.get("skillSlot"))
                if letra:
                    out.setdefault(ev.get("participantId"), []).append(letra)
    return {pid: "".join(seq) for pid, seq in out.items()}


def garantir_colunas(conn):
    """Adiciona as colunas de sinais crus se ainda não existirem (idempotente)."""
    existentes = {r[1] for r in conn.execute("PRAGMA table_info(estatisticas_meta)")}
    for nome, tipo in COLUNAS_NOVAS:
        if nome not in existentes:
            conn.execute(f"ALTER TABLE estatisticas_meta ADD COLUMN {nome} {tipo}")
    conn.commit()


def linha_participante(p: dict, m_id, servidor, elo, div, posicao, apoio, duracao_min,
                       fila, botas_str, runas_str, ordem_skill_str) -> tuple:
    """Constrói a tupla de 44 valores de UM participante (ordem de _SQL_INSERT)."""
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
        # Fila, botas compradas (da timeline), runas (do match) e ordem de skill (da timeline)
        fila, botas_str, runas_str, ordem_skill_str,
    )


def inserir_partida(cursor, data: dict, m_id, servidor, elo, div,
                    fila: str = "solo", timeline: dict = None,
                    elo_por_puuid: dict = None) -> tuple[int, int]:
    """Insere os participantes de UMA partida com a rota inferida.
    `fila` é 'solo' | 'flex' | 'normal'; `timeline` (opcional) é a Match-V5 timeline,
    de onde extraímos as botas realmente compradas por participante.

    `elo_por_puuid` (normal/flex): mapa {puuid: (elo, div)} com o elo REAL de cada
    jogador (ver ranks.py). Quando fornecido, cada participante é rotulado com o
    SEU elo — não o do semente. Um puuid AUSENTE do mapa é descartado (na flex,
    quem não tem rank flex fica de fora). Quando None (solo/apex), todos levam o
    `elo,div` do semente (comportamento original).

    Retorna (inseridos, descartados). NÃO faz commit nem marca partidas_processadas —
    quem chama controla isso (mantém o fluxo atual do crawler)."""
    info = data["info"]
    duracao_min = info["gameDuration"] / 60.0
    rotas = inferir_rotas_partida(info)
    botas_map = _botas_por_participante(timeline) if timeline else {}
    skill_map = _ordem_skill(timeline) if timeline else {}
    inseridos = descartados = 0
    for p in info["participants"]:
        # Elo por jogador (normal/flex) ou rótulo do semente (solo/apex).
        if elo_por_puuid is not None:
            rank = elo_por_puuid.get(p.get("puuid"))
            if rank is None:
                descartados += 1
                continue  # sem elo na fila (ex.: unranked na flex) → fora do dataset
            elo_p, div_p = rank
        else:
            elo_p, div_p = elo, div

        inf = rotas.get(p.get("puuid"), {})
        if not inf.get("confiavel"):
            descartados += 1
            continue  # rota indeduzível (swap/autofill ambíguo) → fora do dataset
        botas_ids = botas_map.get(p.get("participantId"))
        botas_str = ",".join(botas_ids) if botas_ids else None
        runas_str = _runas_de_participante(p)
        ordem_skill_str = skill_map.get(p.get("participantId")) or None
        cursor.execute(
            _SQL_INSERT,
            linha_participante(p, m_id, servidor, elo_p, div_p, inf["rota"], inf.get("apoio"),
                               duracao_min, fila, botas_str, runas_str, ordem_skill_str),
        )
        inseridos += 1
    return inseridos, descartados
