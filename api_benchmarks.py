from fastapi import FastAPI, APIRouter, HTTPException, Query
import sqlite3
import requests
from functools import lru_cache
from collections import Counter
import json
import os
import time
from typing import Optional

app = FastAPI(title="LoL AI Tutor - Benchmarks API")

ARQUIVO_CACHE = "cache_benchmarks.json"
ARQUIVO_CACHE_PANORAMA = "cache_panorama.json"
ARQUIVO_CACHE_ROTA = "cache_benchmarks_rota.json"
ARQUIVO_CACHE_PERCENTIS = "cache_percentis_rota.json"

POSICOES_VALIDAS = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
FILAS_VALIDAS = {"solo", "flex", "normal"}


def _validar_fila(fila: Optional[str]) -> str:
    """Fila do seletor do app (default solo). Valida contra o conjunto suportado."""
    fila = (fila or "solo").lower()
    if fila not in FILAS_VALIDAS:
        raise HTTPException(status_code=400, detail=f"Fila inválida. Use: {', '.join(sorted(FILAS_VALIDAS))}")
    return fila


def _slice_fila(cache: dict, fila: str) -> dict:
    """Os caches agora são NINHADOS por fila: {'solo':{...}, 'flex':{...}, 'normal':{...}}.
    Tolera o formato ANTIGO (plano, sem fila no topo = só solo) durante a janela de deploy:
    antes do 1º ciclo do atualizador novo, o cache ainda pode estar plano."""
    if any(k in cache for k in FILAS_VALIDAS):     # já ninhado
        return cache.get(fila, {})
    return cache if fila == "solo" else {}          # plano antigo = solo

# ==========================================
# ORDEM HIERÁRQUICA DO LEAGUE OF LEGENDS
# ==========================================
ORDEM_ELOS = [
    # UNRANKED (sem divisão, como os elos apex) só existe na fila 'normal'; nas demais
    # filas a chave nunca aparece no cache, então fica de fora naturalmente.
    "UNRANKED_I",
    "IRON_IV", "IRON_III", "IRON_II", "IRON_I",
    "BRONZE_IV", "BRONZE_III", "BRONZE_II", "BRONZE_I",
    "SILVER_IV", "SILVER_III", "SILVER_II", "SILVER_I",
    "GOLD_IV", "GOLD_III", "GOLD_II", "GOLD_I",
    "PLATINUM_IV", "PLATINUM_III", "PLATINUM_II", "PLATINUM_I",
    "EMERALD_IV", "EMERALD_III", "EMERALD_II", "EMERALD_I",
    "DIAMOND_IV", "DIAMOND_III", "DIAMOND_II", "DIAMOND_I",
    "MASTER_I", "GRANDMASTER_I", "CHALLENGER_I"
]

# ==========================================
# ROTAS ESTÁTICAS (CACHE ULTRA RÁPIDO)
# ==========================================

# HEAD explícito: o monitor HTTP do UptimeRobot sonda com HEAD por padrão e o
# FastAPI NÃO deriva HEAD de @app.get sozinho (respondia 405 = incidente falso).
@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    """Sonda de uptime (UptimeRobot etc.). Barata de propósito: não toca o SQLite —
    reporta a idade dos caches que o atualizador regenera; um atualizador morto
    aparece aqui como cache envelhecendo (idade em horas subindo sem parar)."""
    caches = {}
    for nome, arq in (
        ("benchmarks", ARQUIVO_CACHE),
        ("panorama", ARQUIVO_CACHE_PANORAMA),
        ("rota", ARQUIVO_CACHE_ROTA),
        ("percentis", ARQUIVO_CACHE_PERCENTIS),
    ):
        caches[nome] = (
            round((time.time() - os.path.getmtime(arq)) / 3600, 2)
            if os.path.exists(arq)
            else None
        )
    return {"status": "ok", "idade_caches_horas": caches}


def ler_cache(fila: str = "solo"):
    if not os.path.exists(ARQUIVO_CACHE):
        raise HTTPException(status_code=503, detail="Aguarde. O cache inicial está sendo gerado pelo servidor.")

    with open(ARQUIVO_CACHE, "r") as f:
        return _slice_fila(json.load(f), fila)

@app.get("/benchmarks/todos")
def obter_todos_os_benchmarks(fila: Optional[str] = None):
    dados_brutos = ler_cache(_validar_fila(fila))
    dados_ordenados = {}
    
    # 1. Puxa os elos na ordem exata da nossa lista
    for chave in ORDEM_ELOS:
        if chave in dados_brutos:
            dados_ordenados[chave] = dados_brutos[chave]
            
    # 2. Garante que nenhum dado fique de fora (caso a Riot crie um elo novo no futuro)
    for chave in dados_brutos:
        if chave not in dados_ordenados:
            dados_ordenados[chave] = dados_brutos[chave]
            
    return dados_ordenados

# ==========================================
# BENCHMARKS POR ROTA (PERSONALIZADO POR FUNÇÃO)
# Declarados ANTES de /benchmarks/{elo} para o prefixo literal "rota"
# ter prioridade no roteamento do FastAPI.
# ==========================================

def ler_cache_rota(fila: str = "solo"):
    if not os.path.exists(ARQUIVO_CACHE_ROTA):
        raise HTTPException(status_code=503, detail="Aguarde. O cache por rota está sendo gerado pelo servidor.")

    with open(ARQUIVO_CACHE_ROTA, "r") as f:
        return _slice_fila(json.load(f), fila)

def _validar_posicao(posicao: str) -> str:
    posicao = posicao.upper()
    if posicao not in POSICOES_VALIDAS:
        raise HTTPException(
            status_code=400,
            detail=f"Posição inválida. Use uma de: {', '.join(POSICOES_VALIDAS)}"
        )
    return posicao

# Métricas numéricas expostas por rota (mesma união usada pelo cache e pelo frontend).
_METRICAS_ROTA = [
    "kda", "cs_min", "ouro_min", "visao_min", "dano_min", "dano_objetivos",
    "dano_torres", "tempo_cc", "pink_wards", "cura_total", "dano_mitigado",
    "kpa", "solo_kills", "cs_jungle_10m", "cs_rota_10m", "pct_dano_time",
]

def _consultar_agregados(where_extra: str, params: list, fila: str = "solo") -> dict:
    """Núcleo comum: lê o benchmark a partir da tabela PRÉ-AGREGADA estatisticas_agregadas
    (campeao,posicao,elo,divisao,regiao,FILA -> n + somas), combinando o que o filtro pedir.
    AVG = SUM(soma_m)/SUM(n) — matematicamente idêntico ao AVG ao vivo, mas sobre uma
    tabela de ~100k linhas (ms) em vez de varrer os ~4M de estatisticas_meta a cada request.
    Retorna { 'ELO_DIV': {amostra, kda, ...}, ... }. Cai p/ {} se a tabela ainda não existe
    (agregador não rodou) — o chamador trata o 404/fallback."""
    # kpa usa SUM(n_kpa) como divisor (exclui os zeros bugados antigos do crawler); as demais
    # métricas seguem com SUM(n) — o bug era exclusivo do kpa. Ver ID_FIX_KPA no agregador.
    medias = ", ".join(
        (f"SUM(soma_{m}) / SUM(n_kpa) AS {m}" if m == "kpa" else f"SUM(soma_{m}) / SUM(n) AS {m}")
        for m in _METRICAS_ROTA
    )
    query = f"""
        SELECT elo, divisao, SUM(n) AS amostra, {medias}
        FROM estatisticas_agregadas
        WHERE fila = ? AND {where_extra}
        GROUP BY elo, divisao
        HAVING SUM(n) >= 20
    """
    params = [fila] + list(params)
    conn = sqlite3.connect("file:meu_meta_dataset_global.db?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        linhas = conn.execute(query, params).fetchall()
    except sqlite3.OperationalError:
        return {}  # tabela agregada ainda não construída → fallback gracioso
    finally:
        conn.close()

    resultado = {}
    for linha in linhas:
        chave = f"{linha['elo']}_{linha['divisao']}".upper()
        bloco = {"amostra": linha["amostra"]}
        for m in _METRICAS_ROTA:
            valor = linha[m]
            bloco[m] = round(valor, 4) if valor is not None else 0.0
        resultado[chave] = bloco
    return resultado

def _rota_bench_por_regiao(posicao: str, regiao: str, fila: str = "solo") -> dict:
    """Benchmark de uma rota por elo+divisão para UMA região, somando TODOS os campeões
    daquela rota/região na tabela agregada. { 'ELO_DIV': {amostra, kda, ...}, ... }."""
    return _consultar_agregados(
        "UPPER(posicao) = UPPER(?) AND UPPER(regiao) = UPPER(?)",
        [posicao, regiao],
        fila,
    )

def _anexar_percentis(bench: dict, posicao: str, fila: str = "solo") -> dict:
    """Anexa a grade de percentis (p5..p95, cache DIÁRIO do agregador) a cada bloco de
    elo, em bloco['percentis'] = {metrica: [19 valores]}. Os percentis são sempre
    GLOBAIS (sem recorte de região): o pré-agregado regional só guarda somas, e a
    distribuição por elo é estável entre regiões."""
    if not os.path.exists(ARQUIVO_CACHE_PERCENTIS):
        return bench
    with open(ARQUIVO_CACHE_PERCENTIS, "r") as f:
        percentis = _slice_fila(json.load(f), fila)
    for elo, bloco in bench.items():
        grade = percentis.get(elo, {}).get(posicao)
        if grade:
            bloco["percentis"] = {m: v for m, v in grade.items() if m != "amostra"}
            bloco["percentis_amostra"] = grade.get("amostra")
    return bench

def _bench_por_elo(posicao: str, regiao: str = None, fila: str = "solo") -> dict:
    """Retorna { 'ELO_DIV': {metricas} } de uma rota: por região (DB) ou global (cache)."""
    if regiao:
        bench = _rota_bench_por_regiao(posicao, regiao, fila)
    else:
        dados = ler_cache_rota(fila)
        bench = {chave: bloco[posicao] for chave, bloco in dados.items() if posicao in bloco}
    return _anexar_percentis(bench, posicao, fila)

def _bench_por_campeoes(posicao: str, campeoes: list, regiao: str = None, fila: str = "solo") -> dict:
    """Agrega o benchmark de uma rota restrito a uma LISTA de campeões (mono = 1 nome,
    pool = vários), por elo+divisão, direto do DB. Mesma forma do benchmark de rota:
    { 'ELO_DIV': {amostra, kda, ...}, ... }. O AVG sobre a lista já dá a 'média da pool'
    (ponderada por partidas). `regiao` opcional restringe à mesma região."""
    if not campeoes:
        return {}
    placeholders = ", ".join("?" for _ in campeoes)
    where = f"UPPER(posicao) = UPPER(?) AND UPPER(campeao) IN ({placeholders})"
    params = [posicao] + [c.upper() for c in campeoes]
    if regiao:
        where += " AND UPPER(regiao) = UPPER(?)"
        params.append(regiao)
    return _consultar_agregados(where, params, fila)

@app.get("/benchmarks/campeoes/{posicao}")
def obter_benchmark_campeoes(
    posicao: str,
    campeoes: str = Query(..., description="Lista de campeões separada por vírgula (mono = 1)"),
    elo: Optional[str] = None,
    regiao: Optional[str] = None,
    fila: Optional[str] = None,
):
    """Benchmark de uma rota restrito a um conjunto de campeões (mono ou pool do jogador).
    `?campeoes=Jinx,Caitlyn,Ashe`. Sem `?elo=`, retorna todos os elos (ordenados);
    com `?elo=`, faz média das divisões daquele elo (apex ignora divisão)."""
    posicao = _validar_posicao(posicao)
    fila = _validar_fila(fila)
    lista = [c.strip() for c in campeoes.split(",") if c.strip()]
    if not lista:
        raise HTTPException(status_code=400, detail="Informe ao menos um campeão.")

    bench = _bench_por_campeoes(posicao, lista, regiao, fila)
    if not bench:
        raise HTTPException(status_code=404, detail="Amostra insuficiente para esses campeões.")

    if not elo:
        return {chave: bench[chave] for chave in ORDEM_ELOS if chave in bench}

    elo = elo.upper()
    if elo in ["MASTER", "GRANDMASTER", "CHALLENGER", "UNRANKED"]:
        bloco = bench.get(f"{elo}_I")
        if bloco:
            return {"elo": elo, "posicao": posicao, "campeoes": lista, "benchmark": bloco}
        raise HTTPException(status_code=404, detail="Benchmark não encontrado.")

    blocos = [bench[f"{elo}_{d}"] for d in ["I", "II", "III", "IV"] if f"{elo}_{d}" in bench]
    if not blocos:
        raise HTTPException(status_code=404, detail="Benchmark não encontrado.")
    media = {}
    for metrica in blocos[0].keys():
        valores = [b[metrica] for b in blocos if metrica in b]
        media[metrica] = round(sum(valores) / len(valores), 4)
    return {"elo": elo, "posicao": posicao, "campeoes": lista, "benchmark": media}

@app.get("/benchmarks/rota/{posicao}")
def obter_benchmark_rota_todos(posicao: str, regiao: Optional[str] = None, fila: Optional[str] = None):
    """
    Retorna o benchmark de uma rota em TODOS os elos (ordenados hierarquicamente).
    Usado para o cálculo de elo equivalente e para avaliar os elos por rota.
    Com `?regiao=` (ex.: kr), agrega só aquela região direto do DB; sem ele, usa o cache global.
    """
    posicao = _validar_posicao(posicao)
    bench = _bench_por_elo(posicao, regiao, _validar_fila(fila))

    resultado = {chave: bench[chave] for chave in ORDEM_ELOS if chave in bench}

    if not resultado:
        raise HTTPException(status_code=404, detail="Nenhum benchmark encontrado para essa rota/região.")

    return resultado

@app.get("/benchmarks/rota/{posicao}/{elo}")
@app.get("/benchmarks/rota/{posicao}/{elo}/{divisao}")
def obter_benchmark_rota(posicao: str, elo: str, divisao: str = None, regiao: Optional[str] = None, fila: Optional[str] = None):
    """Benchmark de uma rota em um elo (apex ignora divisão; só-elo faz média de I..IV).
    Com `?regiao=` agrega só aquela região; sem ele, usa o cache global."""
    posicao = _validar_posicao(posicao)
    bench = _bench_por_elo(posicao, regiao, _validar_fila(fila))
    elo = elo.upper()

    # Elos Apex: ignoram divisão
    if elo in ["MASTER", "GRANDMASTER", "CHALLENGER", "UNRANKED"]:
        bloco = bench.get(f"{elo}_I")
        if bloco:
            return {"elo": elo, "posicao": posicao, "benchmark": bloco}
        raise HTTPException(status_code=404, detail="Benchmark não encontrado.")

    # Elo + divisão específica
    if divisao:
        bloco = bench.get(f"{elo}_{divisao.upper()}")
        if bloco:
            return {"elo": elo, "divisao": divisao.upper(), "posicao": posicao, "benchmark": bloco}
        raise HTTPException(status_code=404, detail="Benchmark não encontrado.")

    # Apenas elo → média das divisões I..IV que possuem a rota
    blocos = [bench[f"{elo}_{d}"] for d in ["I", "II", "III", "IV"] if f"{elo}_{d}" in bench]

    if not blocos:
        raise HTTPException(status_code=404, detail="Benchmark não encontrado.")

    media = {}
    for metrica in blocos[0].keys():
        # Grades de percentis (listas) não são "mediáveis" entre divisões → ficam de fora.
        valores = [b[metrica] for b in blocos
                   if isinstance(b.get(metrica), (int, float))]
        if valores:
            media[metrica] = round(sum(valores) / len(valores), 4)

    return {"elo": elo, "posicao": posicao, "benchmark": media}

@app.get("/benchmarks/{elo}")
@app.get("/benchmarks/{elo}/{divisao}")
def obter_benchmark(elo: str, divisao: str = None, fila: Optional[str] = None):

    dados = ler_cache(_validar_fila(fila))

    elo = elo.upper()

    # Elos Apex: ignoram divisão
    if elo in ["MASTER", "GRANDMASTER", "CHALLENGER", "UNRANKED"]:

        chave = f"{elo}_I"

        if chave in dados:
            return {
                "elo": elo,
                "benchmark": dados[chave]
            }

        raise HTTPException(
            status_code=404,
            detail="Benchmark não encontrado."
        )

    # Elo + divisão específica
    if divisao:

        chave = f"{elo}_{divisao.upper()}"

        if chave in dados:
            return {
                "elo": elo,
                "divisao": divisao.upper(),
                "benchmark": dados[chave]
            }

        raise HTTPException(
            status_code=404,
            detail="Benchmark não encontrado."
        )

    # Apenas elo → média das divisões I, II, III e IV

    chaves = [
        f"{elo}_I",
        f"{elo}_II",
        f"{elo}_III",
        f"{elo}_IV"
    ]

    benchmarks = [
        dados[chave]
        for chave in chaves
        if chave in dados
    ]

    if not benchmarks:
        raise HTTPException(
            status_code=404,
            detail="Benchmark não encontrado."
        )

    media = {}

    for metrica in benchmarks[0].keys():

        valores = [
            b[metrica]
            for b in benchmarks
            if metrica in b
        ]

        media[metrica] = round(
            sum(valores) / len(valores),
            2
        )

    return {
        "elo": elo,
        "benchmark": media
    }

# ==========================================
# ROTA DINÂMICA (A MINA DE OURO DE DADOS)
# ==========================================

@app.get("/pesquisa-avancada")
def pesquisa_avancada(
    campeao: Optional[str] = None,
    posicao: Optional[str] = None,
    elo: Optional[str] = None,
    divisao: Optional[str] = None,
    regiao: Optional[str] = None,
    vitoria: Optional[int] = None,
    fila: Optional[str] = None
):
    fila = _validar_fila(fila)

    if elo and elo.upper() in ['MASTER', 'GRANDMASTER', 'CHALLENGER']:
        divisao = 'I'

    conn = sqlite3.connect("file:meu_meta_dataset_global.db?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Adicionamos absolutamente TODAS as colunas matemáticas extraídas pelo crawler
    query = """
        SELECT 
            COUNT(*) as total_partidas,
            AVG(vitoria) as win_rate,
            AVG(kda) as kda,
            AVG(cs_min) as cs_min,
            AVG(ouro_min) as ouro_min,
            AVG(visao_min) as visao_min,
            AVG(dano_min) as dano_min,
            AVG(dano_objetivos) as dano_objetivos,
            AVG(dano_torres) as dano_torres,
            AVG(dano_mitigado) as dano_mitigado,
            AVG(cura_total) as cura_total,
            AVG(skillshots_desviadas) as skillshots_desviadas,
            AVG(solo_kills) as solo_kills,
            AVG(pct_dano_time) as pct_dano_time,
            AVG(tempo_cc) as tempo_cc,
            AVG(pink_wards) as pink_wards,
            AVG(tempo_vivo) as tempo_vivo,
            AVG(first_blood) as first_blood,
            AVG(fb_assist) as fb_assist,
            AVG(pings_perigo) as pings_perigo,
            AVG(pings_ajuda) as pings_ajuda,
            AVG(pings_mia) as pings_mia,
            AVG(cs_jungle_10m) as cs_jungle_10m,
            AVG(cs_rota_10m) as cs_rota_10m
        FROM estatisticas_meta
        WHERE COALESCE(fila, 'solo') = ?
    """

    parametros = [fila]
    
    if campeao:
        query += " AND UPPER(campeao) = UPPER(?)"
        parametros.append(campeao)
    if posicao:
        query += " AND UPPER(posicao) = UPPER(?)"
        parametros.append(posicao)
    if elo:
        query += " AND UPPER(elo) = UPPER(?)"
        parametros.append(elo)
    if divisao:
        query += " AND UPPER(divisao) = UPPER(?)"
        parametros.append(divisao)
    if regiao:
        query += " AND UPPER(regiao) = UPPER(?)"
        parametros.append(regiao)
    if vitoria is not None:
        query += " AND vitoria = ?"
        parametros.append(vitoria)
        
    try:
        cursor.execute(query, parametros)
        resultado = cursor.fetchone()
    except Exception as e:
        conn.close()
        return {"erro": f"Erro interno do banco: {str(e)}"}
        
    conn.close()
    
    if not resultado or resultado["total_partidas"] == 0:
        return {"mensagem": "Nenhum dado encontrado para essa combinação de filtros."}
        
    # Agrupamos tudo em categorias lógicas para facilitar a vida do Agente de IA
    return {
        "amostra_partidas": resultado["total_partidas"],
        "taxa_vitoria_porcento": round((resultado["win_rate"] or 0) * 100, 2),
        
        "metricas_basicas": {
            "kda": round(resultado["kda"] or 0, 2),
            "cs_min": round(resultado["cs_min"] or 0, 2),
            "ouro_min": round(resultado["ouro_min"] or 0, 2),
            "visao_min": round(resultado["visao_min"] or 0, 2)
        },
        
        "combate_e_impacto": {
            "dano_min": round(resultado["dano_min"] or 0, 2),
            "dano_objetivos": round(resultado["dano_objetivos"] or 0, 2),
            "dano_torres": round(resultado["dano_torres"] or 0, 2),
            "dano_mitigado": round(resultado["dano_mitigado"] or 0, 2),
            "cura_total": round(resultado["cura_total"] or 0, 2),
            "pct_dano_time_porcento": round((resultado["pct_dano_time"] or 0) * 100, 2)
        },
        
        "early_game_e_agressividade": {
            "first_blood_rate_porcento": round((resultado["first_blood"] or 0) * 100, 2),
            "first_blood_assist_rate_porcento": round((resultado["fb_assist"] or 0) * 100, 2),
            "solo_kills": round(resultado["solo_kills"] or 0, 2),
            "cs_rota_10m": round(resultado["cs_rota_10m"] or 0, 2),
            "cs_jungle_10m": round(resultado["cs_jungle_10m"] or 0, 2)
        },
        
        "utilidade_e_mapa": {
            "pink_wards_compradas": round(resultado["pink_wards"] or 0, 2),
            "tempo_cc_causado": round(resultado["tempo_cc"] or 0, 2),
            "tempo_vivo_segundos": round(resultado["tempo_vivo"] or 0, 2),
            "skillshots_desviadas": round(resultado["skillshots_desviadas"] or 0, 2)
        },
        
        "comunicacao_pings": {
            "pings_perigo": round(resultado["pings_perigo"] or 0, 2),
            "pings_ajuda": round(resultado["pings_ajuda"] or 0, 2),
            "pings_mia": round(resultado["pings_mia"] or 0, 2)
        }
    }

@lru_cache(maxsize=1)
def obter_versao_mais_recente() -> str:
    """Busca a versão mais recente do Data Dragon."""
    try:
        url = "https://ddragon.leagueoflegends.com/api/versions.json"
        versoes = requests.get(url, timeout=10).json()
        return versoes[0]  # A primeira posição é sempre a mais atual
    except Exception as e:
        print(f"Erro ao buscar versão: {e}")
        return "14.11.1" # Fallback caso a API falhe

@lru_cache(maxsize=1)
def obter_mapa_de_itens() -> dict:
    """Busca itens usando a versão dinâmica."""
    versao = obter_versao_mais_recente()
    url_itens = f"https://ddragon.leagueoflegends.com/cdn/{versao}/data/pt_BR/item.json"
    dados = requests.get(url_itens, timeout=10).json()["data"]
    return {item_id: detalhes["name"] for item_id, detalhes in dados.items()}

@app.get("/panorama-meta/{elo}")
def obter_panorama_meta(elo: str, fila: Optional[str] = None):
    """
    Lê o JSON estático com os top 10 campeões e top 5 itens mais frequentes,
    garantindo velocidade extrema para a IA. `?fila=solo|flex|normal` (default solo).
    """
    elo = elo.upper()
    fila = _validar_fila(fila)
    elos_validos = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]
    # UNRANKED só existe na fila normal (jogadores sem rank solo/duo).
    if fila == "normal":
        elos_validos = elos_validos + ["UNRANKED"]

    if elo not in elos_validos:
        raise HTTPException(status_code=400, detail="Elo inválido.")

    if not os.path.exists(ARQUIVO_CACHE_PANORAMA):
        raise HTTPException(
            status_code=503,
            detail="O servidor está construindo o cache do panorama. Tente em alguns minutos."
        )

    try:
        with open(ARQUIVO_CACHE_PANORAMA, "r") as f:
            dados_panorama = _slice_fila(json.load(f), fila)

        if elo in dados_panorama:
            return dados_panorama[elo]
        else:
            return {"mensagem": "Dados insuficientes no banco para gerar o panorama deste Elo."}

    except Exception as e:
         raise HTTPException(status_code=500, detail=f"Erro ao ler cache: {str(e)}")


# ==========================================
# GUIA DO CAMPEÃO (Fase 2) — agregação por campeão/rota/elo
# ==========================================
# Build/botas/feitiços/runas/ordem de skill são MODAIS lidos AO VIVO de estatisticas_meta
# (a tabela pré-agregada só guarda somas de métricas). O índice composto idx_camp_pos_upper
# (UPPER(campeao),UPPER(posicao),elo,divisao) faz cada consulta tocar só a partição do
# campeão/rota/elo — rápido mesmo com ~4M linhas. As métricas-alvo reaproveitam a tabela
# agregada via _bench_por_campeoes. Runas e ordem de skill só existem em linhas novas
# (Fase 1): reportam a PRÓPRIA amostra (linhas não-nulas) e degradam a null enquanto ralas.

LIMIAR_ROTA_GUIA = 20    # rota só entra no seletor de rota com amostra mínima
LIMIAR_GUIA_OK = 30      # abaixo disso o guia marca base_degradada=True


@lru_cache(maxsize=1)
def _mapa_itens_finais_guia() -> dict:
    """id->nome dos itens FINAIS de build (inclui botas), mesmas regras do atualizador
    (obter_mapa_de_itens_finais). Reusa o backup local escrito pelo atualizador."""
    backup = "backup_itens_finais.json"
    try:
        versao = obter_versao_mais_recente()
        dados = requests.get(
            f"https://ddragon.leagueoflegends.com/cdn/{versao}/data/pt_BR/item.json", timeout=10
        ).json()["data"]
        mapa = {}
        for iid, d in dados.items():
            if "into" in d:
                continue
            tags = d.get("tags", [])
            if "Consumable" in tags or "Trinket" in tags or "Vision" in tags:
                continue
            custo = d.get("gold", {}).get("total", 0)
            if custo >= 1500 or ("Boots" in tags and custo > 500):
                mapa[iid] = d["name"]
        return mapa
    except Exception:
        if os.path.exists(backup):
            with open(backup) as f:
                return json.load(f)
        return {}


@lru_cache(maxsize=1)
def _mapa_botas_guia() -> dict:
    """id->nome só das botas tier-2+ (custo > 500), como no atualizador (obter_mapa_botas)."""
    backup = "backup_botas.json"
    try:
        versao = obter_versao_mais_recente()
        dados = requests.get(
            f"https://ddragon.leagueoflegends.com/cdn/{versao}/data/pt_BR/item.json", timeout=10
        ).json()["data"]
        mapa = {
            iid: d["name"] for iid, d in dados.items()
            if "Boots" in d.get("tags", []) and d.get("gold", {}).get("total", 0) > 500
        }
        return mapa
    except Exception:
        if os.path.exists(backup):
            with open(backup) as f:
                return json.load(f)
        return {}


def _metricas_campeao_elo(campeao: str, posicao: str, elo: str, fila: str, regiao: Optional[str]) -> Optional[dict]:
    """Métricas-alvo do campeão na rota, para UM elo (média das divisões I..IV; apex ignora
    divisão). Vem da tabela agregada via _bench_por_campeoes. None se sem amostra."""
    bench = _bench_por_campeoes(posicao, [campeao], regiao, fila)
    elo = elo.upper()
    if elo in ("MASTER", "GRANDMASTER", "CHALLENGER", "UNRANKED"):
        return bench.get(f"{elo}_I")
    blocos = [bench[f"{elo}_{d}"] for d in ("I", "II", "III", "IV") if f"{elo}_{d}" in bench]
    if not blocos:
        return None
    media = {}
    for m in blocos[0].keys():
        vals = [b[m] for b in blocos if isinstance(b.get(m), (int, float))]
        if vals:
            media[m] = round(sum(vals) / len(vals), 4)
    return media


def _agregar_guia(campeao: str, posicao: str, elo: str, fila: str, regiao: Optional[str]) -> dict:
    """Modais de build/botas/feitiços/runas/ordem de skill do campeão numa rota+elo+fila.
    Lê a partição direto de estatisticas_meta (índice idx_camp_pos_upper)."""
    itens_finais = _mapa_itens_finais_guia()
    botas_nomes = _mapa_botas_guia()

    where = "UPPER(campeao)=UPPER(?) AND UPPER(posicao)=UPPER(?) AND elo=?"
    params = [campeao, posicao, elo.upper()]
    if regiao:
        where += " AND UPPER(regiao)=UPPER(?)"
        params.append(regiao)
    where += " AND COALESCE(fila,'solo')=?"
    params.append(fila)

    conn = sqlite3.connect("file:meu_meta_dataset_global.db?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        linhas = conn.execute(
            f"""SELECT itens, botas_compradas, summoner1_id, summoner2_id, runas, ordem_skill
                FROM estatisticas_meta WHERE {where}""",
            params,
        ).fetchall()
    finally:
        conn.close()

    amostra = len(linhas)
    if amostra == 0:
        return {"campeao": campeao, "posicao": posicao, "elo": elo.upper(), "fila": fila, "amostra": 0}

    c_itens, c_botas, c_feit, c_runas, c_skill = Counter(), Counter(), Counter(), Counter(), Counter()
    pos_skill = {"Q": [], "W": [], "E": []}   # índices de level-up p/ prioridade de maximização
    n_runas = n_skill = 0

    for l in linhas:
        # Itens core: inventário final, exclui botas (têm ranking próprio) e itens não-finais.
        itens_str = l["itens"]
        itens_ids = []
        if itens_str:
            itens_ids = json.loads(itens_str) if "[" in itens_str else itens_str.split(",")
            itens_ids = [str(i).strip() for i in itens_ids]
            for iid in itens_ids:
                if iid in itens_finais and iid not in botas_nomes:
                    c_itens[iid] += 1
        # Botas: preferimos as COMPRADAS (timeline); linha antiga cai p/ inventário final.
        botas_str = l["botas_compradas"]
        botas_ids = [b.strip() for b in botas_str.split(",")] if botas_str else itens_ids
        for iid in botas_ids:
            if iid in botas_nomes:
                c_botas[iid] += 1
        # Feitiços de invocador: par não-ordenado.
        s1, s2 = l["summoner1_id"], l["summoner2_id"]
        if s1 and s2:
            c_feit[tuple(sorted((s1, s2)))] += 1
        # Runas (Fase 1): página exata mais comum, sobre a própria amostra não-nula.
        if l["runas"]:
            c_runas[l["runas"]] += 1
            n_runas += 1
        # Ordem de skill (Fase 1): sequência modal + prioridade de maximização Q/W/E.
        skl = l["ordem_skill"]
        if skl:
            n_skill += 1
            c_skill[skl] += 1
            for idx, ch in enumerate(skl):
                if ch in pos_skill:
                    pos_skill[ch].append(idx)

    def _pct(c):
        return round(c / amostra * 100, 1)

    itens = [{"id": i, "nome": itens_finais[i], "uso_pct": _pct(c)} for i, c in c_itens.most_common(6)]
    botas = [{"id": i, "nome": botas_nomes[i], "uso_pct": _pct(c)} for i, c in c_botas.most_common(3)]
    feiticos = [{"ids": list(par), "uso_pct": _pct(c)} for par, c in c_feit.most_common(3)]

    runas = None
    if n_runas:
        rstr, rc = c_runas.most_common(1)[0]
        runas = {"pagina": json.loads(rstr), "uso_pct": round(rc / n_runas * 100, 1), "amostra": n_runas}

    ordem_skill = None
    if n_skill:
        seq, sc = c_skill.most_common(1)[0]
        prioridade = [k for k in sorted(("Q", "W", "E"),
                                        key=lambda k: sum(pos_skill[k]) / len(pos_skill[k]) if pos_skill[k] else 99)
                      if pos_skill[k]]
        ordem_skill = {"sequencia_modal": seq, "uso_pct": round(sc / n_skill * 100, 1),
                       "prioridade": prioridade, "amostra": n_skill}

    return {
        "campeao": campeao, "posicao": posicao, "elo": elo.upper(), "fila": fila,
        "amostra": amostra, "base_degradada": amostra < LIMIAR_GUIA_OK,
        "build": {"itens": itens, "botas": botas},
        "feiticos": feiticos, "runas": runas, "ordem_skill": ordem_skill,
    }


@app.get("/guia-campeao/{campeao}/rotas")
def guia_campeao_rotas(campeao: str, regiao: Optional[str] = None, fila: Optional[str] = None):
    """Rotas em que o campeão tem amostra relevante (para o front decidir o seletor de rota)
    + a rota mais comum (padrão). `?fila=solo|flex|normal` (default solo)."""
    fila = _validar_fila(fila)
    where = "UPPER(campeao)=UPPER(?)"
    params = [campeao]
    if regiao:
        where += " AND UPPER(regiao)=UPPER(?)"
        params.append(regiao)
    where += " AND COALESCE(fila,'solo')=?"
    params.append(fila)

    conn = sqlite3.connect("file:meu_meta_dataset_global.db?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        linhas = conn.execute(
            f"SELECT posicao, COUNT(*) AS n FROM estatisticas_meta WHERE {where} GROUP BY posicao ORDER BY n DESC",
            params,
        ).fetchall()
    finally:
        conn.close()

    rotas = [{"posicao": r["posicao"], "amostra": r["n"]}
             for r in linhas if r["posicao"] and r["n"] >= LIMIAR_ROTA_GUIA]
    if not rotas:
        raise HTTPException(status_code=404, detail="Amostra insuficiente para esse campeão nessa fila.")
    return {"campeao": campeao, "fila": fila, "rota_padrao": rotas[0]["posicao"], "rotas": rotas}


@app.get("/guia-campeao/{campeao}/{posicao}")
def guia_campeao(campeao: str, posicao: str, elo: str = Query(..., description="Elo (tier), ex.: GOLD"),
                 regiao: Optional[str] = None, fila: Optional[str] = None):
    """Guia de UM campeão numa rota+elo+fila: build/botas/feitiços/runas/ordem de skill modais
    + métricas-alvo + amostra. Para o comparativo dual-elo, o front chama duas vezes (elo atual
    e elo acima). `?fila=solo|flex|normal` (default solo)."""
    posicao = _validar_posicao(posicao)
    fila = _validar_fila(fila)
    dados = _agregar_guia(campeao, posicao, elo, fila, regiao)
    if dados["amostra"] == 0:
        raise HTTPException(status_code=404, detail="Sem dados para esse campeão/rota/elo/fila.")
    dados["metricas"] = _metricas_campeao_elo(campeao, posicao, elo, fila, regiao)
    return dados
