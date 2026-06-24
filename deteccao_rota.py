"""
deteccao_rota.py — Inferência da rota REALMENTE jogada por cada participante.

Problema: a match-v5 traz `teamPosition`, que é o rótulo *deduplicado* (a Riot força
1 jogador por rota em cada time). Em lane swap / autofill esse rótulo é atribuído só
pra preencher os 5 slots e NÃO reflete o que o jogador fez de fato — poluindo as
métricas agregadas por rota.

Solução: em vez de confiar num único campo, cruzamos vários sinais independentes da
match-v5 e resolvemos a atribuição do TIME INTEIRO (cada time tem exatamente 1 de cada
rota) escolhendo a combinação jogador<->rota que maximiza a evidência. Aproveitar a
restrição "1 de cada por time" corrige swaps que a análise jogador-a-jogador erraria.

Só descartamos (rota não 'confiavel') quando os sinais não convergem o bastante pra
deduzir a rota com segurança — é o último caso, não o primeiro.

Sinais usados (todos vêm da match-v5, sem chamadas extras):
  - individualPosition : palpite INDEPENDENTE da Riot (sem dedup) — forte
  - teamPosition       : o rótulo deduplicado — moderado
  - lane + role        : sinais crus da timeline (desempate)
  - Smite (summoner1Id/2Id == 11) : evidência física quase definitiva de SELVA
  - jungleCsBefore10Minutes       : confirma/derruba selva
  - laneMinionsFirst10Minutes     : suporte quase não farma minions de rota
"""

from itertools import permutations

ROTAS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
SMITE_ID = 11


def _tem_smite(p: dict) -> bool:
    return SMITE_ID in (p.get("summoner1Id"), p.get("summoner2Id"))


def _lane_da_timeline(p: dict) -> str | None:
    """Mapeia (lane, role) crus da timeline para uma das 5 rotas, quando dá."""
    lane = (p.get("lane") or "").upper()
    role = (p.get("role") or "").upper()
    if lane in ("TOP", "MIDDLE", "JUNGLE"):
        return lane
    if lane == "BOTTOM":
        if "SUPPORT" in role:
            return "UTILITY"
        if "CARRY" in role:
            return "BOTTOM"
    return None


def _votos_jogador(p: dict) -> dict:
    """Pontuação (peso) de cada rota para um participante, somando os sinais.
    Usada como matriz de custo na atribuição do time."""
    chal = p.get("challenges", {}) or {}
    votos = {r: 0.0 for r in ROTAS}

    ip = (p.get("individualPosition") or "").upper()
    if ip in votos:
        votos[ip] += 3.0

    tp = (p.get("teamPosition") or "").upper()
    if tp in votos:
        votos[tp] += 2.0

    lane_rota = _lane_da_timeline(p)
    if lane_rota:
        votos[lane_rota] += 1.5

    # Evidência física da SELVA: Smite é quase definitivo (e selva sem Smite é raríssima).
    if _tem_smite(p):
        votos["JUNGLE"] += 5.0
    else:
        votos["JUNGLE"] -= 3.0
    if chal.get("jungleCsBefore10Minutes", 0) >= 30:
        votos["JUNGLE"] += 2.0

    # Quem farmou muito minion de rota nos 10' iniciais não é o suporte.
    if chal.get("laneMinionsFirst10Minutes", 0) >= 50:
        votos["UTILITY"] -= 2.0

    return votos


def _contar_apoio(p: dict, rota: str) -> int:
    """Quantos sinais INDEPENDENTES concordam com a rota atribuída. É a medida de
    confiança: >= 2 = dá pra confiar; 0 = atribuída só por eliminação (descartar)."""
    chal = p.get("challenges", {}) or {}
    apoio = 0
    if (p.get("individualPosition") or "").upper() == rota:
        apoio += 1
    if (p.get("teamPosition") or "").upper() == rota:
        apoio += 1
    if _lane_da_timeline(p) == rota:
        apoio += 1
    if rota == "JUNGLE" and (_tem_smite(p) or chal.get("jungleCsBefore10Minutes", 0) >= 30):
        apoio += 1
    return apoio


def inferir_rotas_time(participantes_time: list) -> dict:
    """Recebe os participantes de UM time e devolve puuid -> {rota, apoio, confiavel}.
    Resolve a bijeção jogador<->rota que maximiza a soma das votações (5! = 120
    permutações: barato e sem dependências externas)."""
    jogadores = list(participantes_time)
    n = len(jogadores)
    if n == 0:
        return {}
    votos = [_votos_jogador(p) for p in jogadores]
    rotas = ROTAS[:n]

    melhor_perm, melhor_total = rotas, float("-inf")
    for perm in permutations(rotas):
        total = sum(votos[i][perm[i]] for i in range(n))
        if total > melhor_total:
            melhor_total, melhor_perm = total, perm

    resultado = {}
    for i, p in enumerate(jogadores):
        rota = melhor_perm[i]
        apoio = _contar_apoio(p, rota)
        resultado[p.get("puuid", "")] = {
            "rota": rota,
            "apoio": apoio,
            # 'confiavel' = ao menos 2 sinais independentes concordam E a rota tem
            # suporte positivo (não foi imposta contra a evidência, ex.: selva sem Smite).
            "confiavel": apoio >= 2 and votos[i][rota] > 0,
        }
    return resultado


def inferir_rotas_partida(info: dict) -> dict:
    """puuid -> {rota, apoio, confiavel} para os 10 participantes da partida.
    Resolve cada time separadamente (restrição '1 de cada rota por time')."""
    participantes = info.get("participants", [])
    resultado = {}
    for team_id in (100, 200):
        time = [p for p in participantes if p.get("teamId") == team_id]
        if len(time) == 5:
            resultado.update(inferir_rotas_time(time))
        else:
            # Time incompleto (remake/dados atípicos) → sem a restrição, decide
            # por jogador pelo maior voto (e marca confiança pelo apoio).
            for p in time:
                votos = _votos_jogador(p)
                rota = max(votos, key=votos.get)
                apoio = _contar_apoio(p, rota)
                resultado[p.get("puuid", "")] = {
                    "rota": rota, "apoio": apoio,
                    "confiavel": apoio >= 2 and votos[rota] > 0,
                }
    return resultado
