"""
ranks.py — Atribuição de elo POR JOGADOR nas filas normal/flex.

Contexto: nas filas normal e flex, o crawler descobre a partida através de UM
jogador-semente da ladder solo/duo, mas os 10 participantes têm ranks distintos.
Carimbar o elo do semente nos 10 (comportamento antigo) tornava a estatística
dessas filas errada. Aqui resolvemos o elo REAL de cada participante:

  - fila 'normal' → rank RANKED_SOLO_5x5 do jogador (o "elo dele"); sem rank solo
                    o jogador cai no bucket especial UNRANKED.
  - fila 'flex'   → rank RANKED_FLEX_SR do jogador; sem rank flex ele é DESCARTADO
                    daquela partida (UNRANKED existe só na normal, por decisão de produto).
  - fila 'solo'   → inalterado: mantém o rótulo do semente (map = None).

O endpoint league-v4/entries/by-puuid devolve solo E flex numa única chamada, então
um lookup por jogador serve as duas filas. Os resultados vão para um cache PERSISTENTE
sem prazo de validade (arquivo por dev key), evitando re-buscar o mesmo puuid.
"""

import json
import os
import tempfile

UNRANKED = ("UNRANKED", "I")  # bucket único p/ quem não tem rank solo (só na normal)


class RankCache:
    """Cache persistente puuid -> {'solo': (tier,div)|None, 'flex': (tier,div)|None}.

    Sem TTL: rank muda devagar e o objetivo é o dataset agregado, não o rank ao vivo.
    Chave = 'plataforma:puuid' (o lookup league-v4 é por plataforma). Grava em disco
    a cada N novos lookups (atômico via arquivo temporário + rename) e no shutdown."""

    def __init__(self, arquivo: str, flush_cada: int = 50):
        self.arquivo = arquivo
        self.flush_cada = flush_cada
        self._sujo = 0
        self.data: dict = {}
        if os.path.exists(arquivo):
            try:
                with open(arquivo) as f:
                    # JSON não guarda tupla: as listas [tier,div] voltam como tuplas.
                    crua = json.load(f)
                self.data = {
                    k: {q: (tuple(v[q]) if v.get(q) else None) for q in ("solo", "flex")}
                    for k, v in crua.items()
                }
            except Exception:
                self.data = {}  # cache corrompido → recomeça (degradação segura)

    def obter(self, plataforma: str, puuid: str, buscar):
        """Rank do jogador nas duas filas. `buscar(url)` = a chamada_api do crawler
        (respeita o PACER/hot-reload).

        Retorno: {'solo': (tier,div)|None, 'flex': (tier,div)|None} quando a consulta
        teve sucesso (inclusive o [] de quem é unranked → ambos None). Retorna None
        quando o fetch FALHOU (erro de rede/chave): o chamador deve pular o jogador em
        vez de classificá-lo como UNRANKED. A falha não é cacheada (permite nova tentativa)."""
        chave = f"{plataforma}:{puuid}"
        if chave in self.data:
            return self.data[chave]

        url = f"https://{plataforma}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
        resposta = buscar(url)
        if resposta is None:
            return None  # falha transitória: não cacheia e sinaliza "desconhecido"

        resultado = {"solo": None, "flex": None}
        if isinstance(resposta, list):
            for e in resposta:
                qt = e.get("queueType")
                par = (e.get("tier"), e.get("rank"))
                if qt == "RANKED_SOLO_5x5":
                    resultado["solo"] = par
                elif qt == "RANKED_FLEX_SR":
                    resultado["flex"] = par

        self.data[chave] = resultado
        self._sujo += 1
        if self._sujo >= self.flush_cada:
            self.salvar()
        return resultado

    def salvar(self):
        """Persiste o cache de forma atômica (temp + rename no mesmo diretório)."""
        self._sujo = 0
        dir_ = os.path.dirname(os.path.abspath(self.arquivo)) or "."
        try:
            fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.arquivo)
        except Exception:
            pass  # falha ao persistir não pode derrubar a coleta


def construir_mapa_ranks(data: dict, plataforma: str, fila: str, buscar, cache: RankCache):
    """Constrói o mapa {puuid: (elo, div)} usado por inserir_partida na normal/flex.

    - 'solo': retorna None → inserir_partida mantém o rótulo do semente (comportamento atual).
    - 'normal': cada jogador pelo seu rank solo/duo; sem rank solo → UNRANKED.
    - 'flex': cada jogador pelo seu rank flex; sem rank flex o puuid é OMITIDO do mapa
              (inserir_partida descarta quem não está no mapa quando ele é fornecido).
    """
    if fila == "solo":
        return None

    tipo = "solo" if fila == "normal" else "flex"
    mapa: dict = {}
    for p in data.get("info", {}).get("participants", []):
        puuid = p.get("puuid")
        if not puuid:
            continue
        info = cache.obter(plataforma, puuid, buscar)
        if info is None:
            continue  # falha ao buscar o rank → pula (não classifica como UNRANKED)
        rank = info.get(tipo)
        if rank is not None:
            mapa[puuid] = rank
        elif fila == "normal":
            mapa[puuid] = UNRANKED
        # flex sem rank flex → não entra no mapa (será descartado)
    return mapa
