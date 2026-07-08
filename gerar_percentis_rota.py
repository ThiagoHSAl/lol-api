"""Geração ONE-OFF do cache_percentis_rota.json (somente leitura no DB).
Mesma lógica da tarefa percentis_rota do atualizador_cache.py novo — este script
existe só para gerar/validar o cache sem mexer nos serviços em produção."""
import sqlite3, json
from datetime import datetime

DB = "meu_meta_dataset_global.db"
ID_FIX_KPA = 3647311
METRICAS_AGG = [
    "kda", "cs_min", "ouro_min", "visao_min", "dano_min", "dano_objetivos",
    "dano_torres", "tempo_cc", "pink_wards", "cura_total", "dano_mitigado",
    "kpa", "solo_kills", "cs_jungle_10m", "cs_rota_10m", "pct_dano_time",
]

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def conectar(somente_leitura=False):
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    return conn

# --- trecho idêntico ao atualizador_cache.py novo ---
# 3b) PERCENTIS POR ROTA (somente leitura, cadência DIÁRIA)
# ---------------------------------------------------------------------------
# Grade p5..p95 de cada métrica por elo+divisão+posição, para o front dizer
# "melhor que X% das partidas do seu elo" em vez de um rótulo de sub-elo (as
# médias entre elos vizinhos são comprimidas demais para isso). Arquivo SEPARADO
# do cache de médias, que é reescrito a cada hora; a distribuição de dezenas de
# milhares de partidas por grupo é estável, então 1x/dia basta. Sem recorte por
# região: o pré-agregado regional só guarda somas, e percentil exige a amostra.
INTERVALO_PERCENTIS = 86400
PONTOS_PERCENTIS = list(range(5, 100, 5))  # p5, p10, ..., p95


def _grade_percentis(valores_ordenados: list) -> list:
    """Valores ORDENADOS → valor em cada ponto de PONTOS_PERCENTIS (interp. linear)."""
    n = len(valores_ordenados)
    grade = []
    for p in PONTOS_PERCENTIS:
        i = (n - 1) * (p / 100)
        lo = int(i)
        hi = min(lo + 1, n - 1)
        v = valores_ordenados[lo] + (valores_ordenados[hi] - valores_ordenados[lo]) * (i - lo)
        grade.append(round(v, 4))
    return grade


def atualizar_cache_percentis_rota():
    """Percentis por (elo, divisão, posição). Uma query por grupo (≤ ~50k linhas de
    cada vez, via idx_elo_divisao) para caber na RAM do servidor."""
    conn = conectar(somente_leitura=True)
    try:
        grupos = conn.execute("""
            SELECT elo, divisao, posicao, COUNT(*) AS n
            FROM estatisticas_meta
            WHERE elo IS NOT NULL AND divisao IS NOT NULL
              AND posicao IS NOT NULL AND posicao <> ''
            GROUP BY elo, divisao, posicao
            HAVING COUNT(*) >= 100
        """).fetchall()

        colunas = ", ".join(METRICAS_AGG)
        resultado = {}
        for g in grupos:
            linhas = conn.execute(
                f"SELECT id, {colunas} FROM estatisticas_meta "
                "WHERE elo = ? AND divisao = ? AND posicao = ?",
                (g["elo"], g["divisao"], g["posicao"]),
            ).fetchall()
            bloco = {"amostra": len(linhas)}
            for m in METRICAS_AGG:
                if m == "kpa":  # mesma regra do AVG: zeros bugados antigos fora (ver ID_FIX_KPA)
                    vals = sorted(l["kpa"] for l in linhas
                                  if l["kpa"] is not None and (l["kpa"] > 0 or l["id"] >= ID_FIX_KPA))
                else:
                    vals = sorted(l[m] for l in linhas if l[m] is not None)
                if len(vals) >= 100:
                    bloco[m] = _grade_percentis(vals)
            elo_completo = f"{g['elo']}_{g['divisao']}".upper()
            resultado.setdefault(elo_completo, {})[str(g["posicao"]).upper()] = bloco
    finally:
        conn.close()

    with open("cache_percentis_rota.json", "w") as f:
        json.dump(resultado, f)
    log("✅ Cache de Percentis por Rota atualizado!")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    atualizar_cache_percentis_rota()
