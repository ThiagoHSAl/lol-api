import sqlite3
import json
import time
import requests
import os
import threading
from datetime import datetime
from collections import Counter

DB = "meu_meta_dataset_global.db"
INTERVALO = 3600  # cadência-alvo de cada tarefa (medida a partir do INÍCIO de cada ciclo)

# Fronteira do bug de KPA. Os crawlers liam `chal.get('kpa', 0)` em vez de
# `chal.get('killParticipation', 0)` — a chave 'kpa' nunca existia, então TODA linha antiga
# entrou com kpa=0 (commit 56e4109, 2026-06-21, corrigiu o coletor). Sem coluna de data, a
# fronteira é o `id`: a primeira linha com kpa real é id 3.647.311 (não há nenhum kpa>0 antes
# disso, e ~3,65M zeros formam o prefixo bugado). Regra: um kpa entra na média sse kpa>0 OU
# id>=ID_FIX_KPA — assim os zeros bugados antigos saem do cálculo, mas qualquer kpa=0 legítimo
# coletado daqui pra frente (id>=fronteira) continua contando normalmente.
ID_FIX_KPA = 3647311

# Métricas numéricas agregadas (mesma união usada pelos endpoints por rota/campeão).
METRICAS_AGG = [
    "kda", "cs_min", "ouro_min", "visao_min", "dano_min", "dano_objetivos",
    "dano_torres", "tempo_cc", "pink_wards", "cura_total", "dano_mitigado",
    "kpa", "solo_kills", "cs_jungle_10m", "cs_rota_10m", "pct_dano_time",
]


# Filas suportadas. Os caches passam a ser NINHADOS por fila: {"solo":{...}, "flex":{...},
# "normal":{...}}. 'solo' inclui os 5M de registros antigos (fila NULL, pré-migração);
# flex/normal só existem em dados novos (crawlers desde 08/07). Ver ingest_crawler.
FILAS = ["solo", "flex", "normal"]


def _cond_fila(fila: str) -> str:
    """Fragmento WHERE para restringir a agregação a UMA fila. 'solo' precisa do COALESCE
    para incluir os registros antigos (fila NULL); flex/normal são sempre explícitos."""
    return "COALESCE(fila, 'solo') = 'solo'" if fila == "solo" else f"fila = '{fila}'"


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def conectar(somente_leitura=False):
    """Cada tarefa abre a SUA conexão. Em WAL, N leitores rodam em paralelo + 1 escritor;
    as tarefas de cache (benchmarks/rota/panorama) são somente-leitura e nunca tomam lock,
    então jamais seguram os crawlers nem umas às outras."""
    if somente_leitura:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30.0)
    else:
        conn = sqlite3.connect(DB, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


# ---------------------------------------------------------------------------
# 1) TABELA AGREGADA — a ÚNICA tarefa que escreve no banco.
# ---------------------------------------------------------------------------
def atualizar_tabela_agregada():
    """Materializa estatisticas_agregadas: (campeao,posicao,elo,divisao,regiao) -> n + somas
    de cada métrica. É a FONTE ÚNICA dos benchmarks por região (rota) e por campeão/pool — os
    endpoints viram SUM(soma)/SUM(n) sobre esta tabela pequena (~70k linhas), em vez de AVG ao
    vivo sobre os ~4M de estatisticas_meta.

    ANTES: 'CREATE TABLE AS SELECT' (varredura+GROUP BY de 4M linhas, ~12min) rodava DENTRO de
    uma transação de escrita, segurando o write-lock o tempo todo → travava os crawlers por
    ~12min. AGORA, separamos em duas fases:
      • FASE LEITURA (cara): roda o GROUP BY como SELECT puro numa conexão somente-leitura.
        Em WAL, leitura NÃO toma write-lock → os crawlers escrevem livremente durante os ~12min.
      • FASE SWAP (rápida): uma transação curta (segundos) faz DROP+CREATE+executemany das ~70k
        linhas já calculadas + índices. O write-lock fica retido só por segundos.
    Os leitores (API) seguem vendo a tabela ANTIGA até o COMMIT, sem janela de 'tabela inexistente'."""
    somas = ", ".join(f"SUM({m}) AS soma_{m}" for m in METRICAS_AGG)
    # n_kpa: denominador SÓ do kpa — conta as linhas elegíveis (kpa>0 OU pós-fix), ignorando
    # os zeros bugados antigos. soma_kpa já está correto (zero bugado soma 0); só o divisor
    # precisava deixar de incluir o prefixo bugado. Os demais n/soma seguem sobre TODAS as
    # linhas (o bug era exclusivo do kpa).
    # Agora com a dimensão FILA (COALESCE p/ os antigos NULL virarem 'solo'): a mesma tabela
    # serve as 3 filas de uma vez, e os endpoints filtram por fila.
    select_agg = f"""
        SELECT campeao, posicao, elo, divisao, regiao, COALESCE(fila, 'solo') AS fila,
               COUNT(*) AS n,
               SUM(CASE WHEN kpa > 0 OR id >= {ID_FIX_KPA} THEN 1 ELSE 0 END) AS n_kpa,
               {somas}
        FROM estatisticas_meta
        WHERE elo IS NOT NULL AND divisao IS NOT NULL
          AND posicao IS NOT NULL AND posicao <> ''
          AND regiao IS NOT NULL AND campeao IS NOT NULL
        GROUP BY campeao, posicao, elo, divisao, regiao, COALESCE(fila, 'solo')
    """

    # FASE LEITURA — pesada, mas sem write-lock (não trava crawlers).
    ro = conectar(somente_leitura=True)
    try:
        linhas = ro.execute(select_agg).fetchall()
    finally:
        ro.close()

    cols = ["campeao", "posicao", "elo", "divisao", "regiao", "fila", "n", "n_kpa"] + [f"soma_{m}" for m in METRICAS_AGG]
    coldefs = ("campeao TEXT, posicao TEXT, elo TEXT, divisao TEXT, regiao TEXT, fila TEXT, n INTEGER, n_kpa INTEGER, "
               + ", ".join(f"soma_{m} REAL" for m in METRICAS_AGG))
    placeholders = ", ".join("?" * len(cols))
    dados = [tuple(linha) for linha in linhas]

    # FASE SWAP — curta: write-lock retido só por segundos.
    rw = conectar()
    try:
        rw.execute("BEGIN IMMEDIATE")
        rw.execute("DROP TABLE IF EXISTS estatisticas_agregadas")
        rw.execute(f"CREATE TABLE estatisticas_agregadas ({coldefs})")
        rw.executemany(
            f"INSERT INTO estatisticas_agregadas ({', '.join(cols)}) VALUES ({placeholders})",
            dados,
        )
        # Índices incluem `fila` como 1ª coluna: os endpoints sempre filtram por ela.
        rw.execute("""CREATE INDEX idx_agg_pos_reg
                      ON estatisticas_agregadas(fila, UPPER(posicao), UPPER(regiao), elo, divisao)""")
        rw.execute("""CREATE INDEX idx_agg_camp_pos_reg
                      ON estatisticas_agregadas(fila, UPPER(campeao), UPPER(posicao), UPPER(regiao), elo, divisao)""")
        rw.execute("COMMIT")
        # Mantém o WAL pequeno: trunca o que for possível (não bloqueia se houver leitor ativo).
        rw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        rw.execute("ROLLBACK")
        raise
    finally:
        rw.close()
    log(f"✅ Tabela de agregados reconstruída ({len(dados)} linhas).")


def obter_mapa_de_itens_finais() -> dict:
    """Busca o nome dos itens. Salva em cache local como plano de backup."""
    arquivo_backup = "backup_itens_finais.json"

    try:
        url_versoes = "https://ddragon.leagueoflegends.com/api/versions.json"
        patch_atual = requests.get(url_versoes, timeout=5).json()[0]
        url_itens = f"https://ddragon.leagueoflegends.com/cdn/{patch_atual}/data/pt_BR/item.json"
        dados_itens = requests.get(url_itens, timeout=10).json()["data"]

        mapa_itens_finais = {}
        for item_id, detalhes in dados_itens.items():
            if "into" in detalhes: continue

            tags = detalhes.get("tags", [])
            if "Consumable" in tags or "Trinket" in tags or "Vision" in tags: continue

            custo_total = detalhes.get("gold", {}).get("total", 0)
            is_bota = "Boots" in tags

            if custo_total >= 1500 or (is_bota and custo_total > 500):
                mapa_itens_finais[item_id] = detalhes["name"]

        # SUCESSO! Salva o resultado no disco como um backup de segurança
        with open(arquivo_backup, "w") as f:
            json.dump(mapa_itens_finais, f)

        return mapa_itens_finais

    except Exception as e:
        print(f"⚠️ Servidor da Riot falhou ({e}). Tentando usar o cache local...")

        # PLANO B: Se a internet cair ou a Riot travar, usamos o último cache salvo
        if os.path.exists(arquivo_backup):
            with open(arquivo_backup, "r") as f:
                return json.load(f)
        else:
            print("❌ Nenhum cache local encontrado.")
            return {}


def obter_mapa_botas() -> dict:
    """Mapa id -> nome só das BOTAS compráveis (tier 2+, custo > 500 — exclui a Bota
    básica de 300). O panorama conta as botas num ranking próprio ('top_2_botas'),
    separado dos itens finais. Mesmo padrão de backup local do mapa de itens."""
    arquivo_backup = "backup_botas.json"
    try:
        url_versoes = "https://ddragon.leagueoflegends.com/api/versions.json"
        patch_atual = requests.get(url_versoes, timeout=5).json()[0]
        url_itens = f"https://ddragon.leagueoflegends.com/cdn/{patch_atual}/data/pt_BR/item.json"
        dados_itens = requests.get(url_itens, timeout=10).json()["data"]

        mapa_botas = {
            item_id: detalhes["name"]
            for item_id, detalhes in dados_itens.items()
            if "Boots" in detalhes.get("tags", [])
            and detalhes.get("gold", {}).get("total", 0) > 500
        }
        with open(arquivo_backup, "w") as f:
            json.dump(mapa_botas, f)
        return mapa_botas
    except Exception as e:
        print(f"⚠️ Servidor da Riot falhou ({e}). Tentando usar o cache local de botas...")
        if os.path.exists(arquivo_backup):
            with open(arquivo_backup, "r") as f:
                return json.load(f)
        print("❌ Nenhum cache local de botas encontrado.")
        return {}


# ---------------------------------------------------------------------------
# 2) BENCHMARKS BASE (somente leitura)
# ---------------------------------------------------------------------------
def atualizar_cache_benchmarks():
    """Agregação de métricas base por elo+divisão, NINHADA por fila."""
    conn = conectar(somente_leitura=True)
    try:
        por_fila = {}
        for fila in FILAS:
            query = f"""
                SELECT elo || '_' || divisao AS elo_completo,
                       AVG(kda) AS kda, AVG(cs_min) AS cs_min, AVG(ouro_min) AS ouro_min, AVG(visao_min) AS visao_min
                FROM estatisticas_meta
                WHERE elo IS NOT NULL AND divisao IS NOT NULL
                  AND {_cond_fila(fila)}
                GROUP BY elo, divisao
            """
            resultado_json = {}
            for linha in conn.execute(query).fetchall():
                elo_completo = str(linha["elo_completo"]).upper()
                resultado_json[elo_completo] = {
                    "kda": round(linha["kda"], 2) if linha["kda"] else 0.0,
                    "cs_min": round(linha["cs_min"], 2) if linha["cs_min"] else 0.0,
                    "ouro_min": round(linha["ouro_min"], 2) if linha["ouro_min"] else 0.0,
                    "visao_min": round(linha["visao_min"], 2) if linha["visao_min"] else 0.0,
                }
            por_fila[fila] = resultado_json
    finally:
        conn.close()

    with open("cache_benchmarks.json", "w") as f:
        json.dump(por_fila, f, indent=4)
    log("✅ Cache de Benchmarks Base atualizado!")


# ---------------------------------------------------------------------------
# 3) BENCHMARKS POR ROTA (somente leitura)
# ---------------------------------------------------------------------------
def atualizar_cache_benchmarks_rota():
    """Agrega benchmarks por elo + divisao + POSICAO, com todas as métricas relevantes a cada rota."""
    metricas = [
        "kda", "cs_min", "ouro_min", "visao_min", "dano_min", "dano_objetivos",
        "dano_torres", "tempo_cc", "pink_wards", "cura_total", "dano_mitigado",
        "kpa", "solo_kills", "cs_jungle_10m", "cs_rota_10m", "pct_dano_time",
    ]
    conn = conectar(somente_leitura=True)
    try:
        por_fila = {}
        for fila in FILAS:
            query = f"""
                SELECT elo, divisao, posicao, COUNT(*) AS amostra,
                       AVG(kda) AS kda, AVG(cs_min) AS cs_min, AVG(ouro_min) AS ouro_min,
                       AVG(visao_min) AS visao_min, AVG(dano_min) AS dano_min,
                       AVG(dano_objetivos) AS dano_objetivos, AVG(dano_torres) AS dano_torres,
                       AVG(tempo_cc) AS tempo_cc, AVG(pink_wards) AS pink_wards,
                       AVG(cura_total) AS cura_total, AVG(dano_mitigado) AS dano_mitigado,
                       AVG(CASE WHEN kpa > 0 OR id >= {ID_FIX_KPA} THEN kpa END) AS kpa, AVG(solo_kills) AS solo_kills,
                       AVG(cs_jungle_10m) AS cs_jungle_10m, AVG(cs_rota_10m) AS cs_rota_10m,
                       AVG(pct_dano_time) AS pct_dano_time
                FROM estatisticas_meta
                WHERE elo IS NOT NULL AND divisao IS NOT NULL
                  AND posicao IS NOT NULL AND posicao <> ''
                  AND {_cond_fila(fila)}
                GROUP BY elo, divisao, posicao
                HAVING COUNT(*) >= 20
            """
            resultado_json = {}
            for linha in conn.execute(query).fetchall():
                elo_completo = f"{linha['elo']}_{linha['divisao']}".upper()
                posicao = str(linha["posicao"]).upper()
                bloco = {"amostra": linha["amostra"]}
                for m in metricas:
                    valor = linha[m]
                    bloco[m] = round(valor, 4) if valor is not None else 0.0
                resultado_json.setdefault(elo_completo, {})[posicao] = bloco
            por_fila[fila] = resultado_json
    finally:
        conn.close()

    resultado_json = por_fila
    with open("cache_benchmarks_rota.json", "w") as f:
        json.dump(resultado_json, f, indent=4)
    log("✅ Cache de Benchmarks por Rota atualizado!")


# ---------------------------------------------------------------------------
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
    colunas = ", ".join(METRICAS_AGG)
    conn = conectar(somente_leitura=True)
    try:
        por_fila = {}
        for fila in FILAS:
            grupos = conn.execute(f"""
                SELECT elo, divisao, posicao, COUNT(*) AS n
                FROM estatisticas_meta
                WHERE elo IS NOT NULL AND divisao IS NOT NULL
                  AND posicao IS NOT NULL AND posicao <> ''
                  AND {_cond_fila(fila)}
                GROUP BY elo, divisao, posicao
                HAVING COUNT(*) >= 100
            """).fetchall()

            resultado = {}
            for g in grupos:
                linhas = conn.execute(
                    f"SELECT id, {colunas} FROM estatisticas_meta "
                    f"WHERE elo = ? AND divisao = ? AND posicao = ? AND {_cond_fila(fila)}",
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
            por_fila[fila] = resultado
    finally:
        conn.close()

    with open("cache_percentis_rota.json", "w") as f:
        json.dump(por_fila, f)
    log("✅ Cache de Percentis por Rota atualizado!")


# ---------------------------------------------------------------------------
# 4) PANORAMA META (somente leitura) — otimizado
# ---------------------------------------------------------------------------
def atualizar_cache_panorama():
    """Top 10 campeões + build ideal (itens completos) por elo/rota.

    ANTES: para CADA campeão do top-10 disparava um 'SELECT itens WHERE campeao=?' que, via
    idx_campeao, lia TODAS as linhas do campeão (todos elos/regiões) e filtrava depois — ~500
    varreduras enormes por ciclo, levando horas. AGORA, apoiado no índice composto
    (elo,posicao,campeao) e sem UPPER() (os dados já estão em maiúsculas):
      • o top-10 vira um range scan indexado da partição (elo,posicao);
      • os itens são lidos em LOTE, uma query por combo com 'campeao IN (top10)', tocando só
        as linhas daquele elo/rota dos 10 campeões — em vez de 1 varredura por campeão."""
    mapa_itens_finais = obter_mapa_de_itens_finais()
    # Botas têm ranking PRÓPRIO ('top_2_botas') e saem do top-5 de itens — sem isso
    # elas competiam pelos 5 slots e o card não tinha a informação de bota separada.
    mapa_botas = obter_mapa_botas()
    elos = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]
    posicoes = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]

    conn = conectar(somente_leitura=True)

    def _top10_da_fila(fila, elo, posicao):
        """Top-10 campeões (por Wilson LB) + build de UMA fila/elo/posição."""
        campeoes_db = conn.execute(
            f"""
            SELECT campeao, COUNT(*) AS total_partidas,
                   SUM(vitoria) * 100.0 / COUNT(*) AS winrate,
                   ( ( (SUM(vitoria)*1.0/COUNT(*)) + 5.4289/(2*COUNT(*))
                       - 2.33*sqrt( ((SUM(vitoria)*1.0/COUNT(*))*(1-(SUM(vitoria)*1.0/COUNT(*)))
                                    + 5.4289/(4*COUNT(*)))/COUNT(*) ) )
                     / (1 + 5.4289/COUNT(*)) ) AS wilson_lb
            FROM estatisticas_meta
            WHERE elo = ? AND posicao = ? AND {_cond_fila(fila)}
            GROUP BY campeao
            HAVING total_partidas >= 100
            ORDER BY wilson_lb DESC
            LIMIT 10
            """,
            (elo, posicao),
        ).fetchall()

        top_campeoes = [r["campeao"] for r in campeoes_db]
        contadores = {c: Counter() for c in top_campeoes}
        contadores_botas = {c: Counter() for c in top_campeoes}

        if top_campeoes:
            placeholders = ", ".join("?" * len(top_campeoes))
            cursor_itens = conn.execute(
                f"""
                SELECT campeao, itens, botas_compradas FROM estatisticas_meta
                WHERE elo = ? AND posicao = ? AND campeao IN ({placeholders})
                  AND {_cond_fila(fila)}
                """,
                (elo, posicao, *top_campeoes),
            )
            for linha in cursor_itens:
                contador = contadores[linha["campeao"]]
                contador_botas = contadores_botas[linha["campeao"]]

                # TOP-5 ITENS: sempre do inventário final (itens de build completa
                # não sofrem do problema da bota vendida no late).
                itens_str = linha["itens"]
                itens_ids = []
                if itens_str:
                    itens_ids = json.loads(itens_str) if "[" in itens_str else itens_str.split(",")
                    itens_ids = [str(i).strip() for i in itens_ids]
                    for item_id in itens_ids:
                        if item_id in mapa_itens_finais:
                            contador[item_id] += 1

                # TOP-2 BOTAS com FALLBACK: prefere as botas COMPRADAS (timeline, sem
                # viés); nas linhas antigas (sem timeline) cai para o inventário final.
                # mapa_botas só tem tier-2 (>500g), então a Bota básica 1001 é ignorada.
                botas_str = linha["botas_compradas"]
                botas_ids = ([b.strip() for b in botas_str.split(",")]
                             if botas_str else itens_ids)
                for item_id in botas_ids:
                    if item_id in mapa_botas:
                        contador_botas[item_id] += 1

        top_10 = []
        for champ_row in campeoes_db:
            campeao = champ_row["campeao"]
            top_5_ids = [item_id for item_id, _ in contadores[campeao].most_common(5)]
            top_5_nomes = [mapa_itens_finais[i] for i in top_5_ids]
            top_2_botas = [mapa_botas[i] for i, _ in
                           contadores_botas[campeao].most_common(2)]
            top_10.append({
                "campeao": campeao,
                "winrate": round(champ_row["winrate"], 2),
                "amostra": champ_row["total_partidas"],
                "top_5_itens": top_5_nomes,
                "top_2_botas": top_2_botas,
            })
        return top_10

    try:
        por_fila = {}
        for fila in FILAS:
            panorama_global = {}
            for elo in elos:
                panorama_global[elo] = {pos: _top10_da_fila(fila, elo, pos) for pos in posicoes}
            por_fila[fila] = panorama_global
    finally:
        conn.close()

    with open("cache_panorama.json", "w") as f:
        json.dump(por_fila, f, indent=4)
    log("✅ Cache do Panorama Meta atualizado!")


# ---------------------------------------------------------------------------
# Orquestração: cada tarefa em SEU loop/thread, com cadência própria.
# Uma tarefa lenta só atrasa a si mesma — nunca segura as outras.
# ---------------------------------------------------------------------------
def loop_tarefa(nome, funcao, intervalo=INTERVALO):
    while True:
        inicio = time.time()
        try:
            funcao()
        except Exception as e:
            log(f"❌ Erro na tarefa '{nome}': {e}")
        dormir = max(0, intervalo - (time.time() - inicio))
        time.sleep(dormir)


def iniciar_agregacao():
    # Garante o índice que sustenta o panorama (idempotente; instantâneo se já existir).
    try:
        c = conectar()
        c.execute("PRAGMA busy_timeout=120000")
        c.execute("CREATE INDEX IF NOT EXISTS idx_elo_pos_camp ON estatisticas_meta(elo, posicao, campeao)")
        c.close()
    except Exception as e:
        log(f"⚠️ Não foi possível garantir idx_elo_pos_camp: {e}")

    tarefas = [
        ("agregados", atualizar_tabela_agregada, INTERVALO),
        ("benchmarks", atualizar_cache_benchmarks, INTERVALO),
        ("benchmarks_rota", atualizar_cache_benchmarks_rota, INTERVALO),
        ("percentis_rota", atualizar_cache_percentis_rota, INTERVALO_PERCENTIS),
        ("panorama", atualizar_cache_panorama, INTERVALO),
    ]
    log("Iniciando processamento de caches em PARALELO (5 tarefas independentes)...")
    threads = [threading.Thread(target=loop_tarefa, args=(n, f, i), daemon=True, name=n) for n, f, i in tarefas]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    iniciar_agregacao()
