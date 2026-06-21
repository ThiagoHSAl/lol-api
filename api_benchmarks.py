from fastapi import FastAPI, APIRouter, HTTPException, Query
import sqlite3
import requests
from functools import lru_cache
import json
import os
from typing import Optional

app = FastAPI(title="LoL AI Tutor - Benchmarks API")

ARQUIVO_CACHE = "cache_benchmarks.json"
ARQUIVO_CACHE_PANORAMA = "cache_panorama.json"
ARQUIVO_CACHE_ROTA = "cache_benchmarks_rota.json"

POSICOES_VALIDAS = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]

# ==========================================
# ORDEM HIERÁRQUICA DO LEAGUE OF LEGENDS
# ==========================================
ORDEM_ELOS = [
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

def ler_cache():
    if not os.path.exists(ARQUIVO_CACHE):
        raise HTTPException(status_code=503, detail="Aguarde. O cache inicial está sendo gerado pelo servidor.")
    
    with open(ARQUIVO_CACHE, "r") as f:
        return json.load(f)

@app.get("/benchmarks/todos")
def obter_todos_os_benchmarks():
    dados_brutos = ler_cache()
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

def ler_cache_rota():
    if not os.path.exists(ARQUIVO_CACHE_ROTA):
        raise HTTPException(status_code=503, detail="Aguarde. O cache por rota está sendo gerado pelo servidor.")

    with open(ARQUIVO_CACHE_ROTA, "r") as f:
        return json.load(f)

def _validar_posicao(posicao: str) -> str:
    posicao = posicao.upper()
    if posicao not in POSICOES_VALIDAS:
        raise HTTPException(
            status_code=400,
            detail=f"Posição inválida. Use uma de: {', '.join(POSICOES_VALIDAS)}"
        )
    return posicao

@app.get("/benchmarks/rota/{posicao}")
def obter_benchmark_rota_todos(posicao: str):
    """
    Retorna o benchmark de uma rota em TODOS os elos (ordenados hierarquicamente).
    Usado para o cálculo de elo equivalente e para avaliar os elos por rota.
    """
    posicao = _validar_posicao(posicao)
    dados = ler_cache_rota()

    resultado = {}
    for chave in ORDEM_ELOS:
        bloco = dados.get(chave, {})
        if posicao in bloco:
            resultado[chave] = bloco[posicao]

    if not resultado:
        raise HTTPException(status_code=404, detail="Nenhum benchmark encontrado para essa rota.")

    return resultado

@app.get("/benchmarks/rota/{posicao}/{elo}")
@app.get("/benchmarks/rota/{posicao}/{elo}/{divisao}")
def obter_benchmark_rota(posicao: str, elo: str, divisao: str = None):
    """Benchmark de uma rota em um elo (apex ignora divisão; só-elo faz média de I..IV)."""
    posicao = _validar_posicao(posicao)
    dados = ler_cache_rota()
    elo = elo.upper()

    # Elos Apex: ignoram divisão
    if elo in ["MASTER", "GRANDMASTER", "CHALLENGER"]:
        bloco = dados.get(f"{elo}_I", {})
        if posicao in bloco:
            return {"elo": elo, "posicao": posicao, "benchmark": bloco[posicao]}
        raise HTTPException(status_code=404, detail="Benchmark não encontrado.")

    # Elo + divisão específica
    if divisao:
        bloco = dados.get(f"{elo}_{divisao.upper()}", {})
        if posicao in bloco:
            return {"elo": elo, "divisao": divisao.upper(), "posicao": posicao, "benchmark": bloco[posicao]}
        raise HTTPException(status_code=404, detail="Benchmark não encontrado.")

    # Apenas elo → média das divisões I..IV que possuem a rota
    blocos = [
        dados[f"{elo}_{d}"][posicao]
        for d in ["I", "II", "III", "IV"]
        if f"{elo}_{d}" in dados and posicao in dados[f"{elo}_{d}"]
    ]

    if not blocos:
        raise HTTPException(status_code=404, detail="Benchmark não encontrado.")

    media = {}
    for metrica in blocos[0].keys():
        valores = [b[metrica] for b in blocos if metrica in b]
        media[metrica] = round(sum(valores) / len(valores), 4)

    return {"elo": elo, "posicao": posicao, "benchmark": media}

@app.get("/benchmarks/{elo}")
@app.get("/benchmarks/{elo}/{divisao}")
def obter_benchmark(elo: str, divisao: str = None):

    dados = ler_cache()

    elo = elo.upper()

    # Elos Apex: ignoram divisão
    if elo in ["MASTER", "GRANDMASTER", "CHALLENGER"]:

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
    vitoria: Optional[int] = None
):

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
        WHERE 1=1
    """
    
    parametros = []
    
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
def obter_panorama_meta(elo: str):
    """
    Lê o JSON estático com os top 10 campeões e top 5 itens mais frequentes, 
    garantindo velocidade extrema para a IA.
    """
    elo = elo.upper()
    elos_validos = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]
    
    if elo not in elos_validos:
        raise HTTPException(status_code=400, detail="Elo inválido.")

    if not os.path.exists(ARQUIVO_CACHE_PANORAMA):
        raise HTTPException(
            status_code=503, 
            detail="O servidor está construindo o cache do panorama. Tente em alguns minutos."
        )

    try:
        with open(ARQUIVO_CACHE_PANORAMA, "r") as f:
            dados_panorama = json.load(f)
            
        if elo in dados_panorama:
            return dados_panorama[elo]
        else:
            return {"mensagem": "Dados insuficientes no banco para gerar o panorama deste Elo."}
            
    except Exception as e:
         raise HTTPException(status_code=500, detail=f"Erro ao ler cache: {str(e)}")
