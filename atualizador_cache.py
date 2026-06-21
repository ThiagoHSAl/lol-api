import sqlite3
import json
import time
import requests
import os
from datetime import datetime
from collections import Counter

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

def atualizar_cache_benchmarks(conn):
    """Sua função original de agregação de métricas."""
    cursor = conn.cursor()
    query = """
        SELECT elo || '_' || divisao AS elo_completo,
               AVG(kda) AS kda, AVG(cs_min) AS cs_min, AVG(ouro_min) AS ouro_min, AVG(visao_min) AS visao_min
        FROM estatisticas_meta
        WHERE elo IS NOT NULL AND divisao IS NOT NULL
        GROUP BY elo, divisao
    """
    cursor.execute(query)
    linhas = cursor.fetchall()

    resultado_json = {}
    for linha in linhas:
        elo_completo = str(linha["elo_completo"]).upper()
        resultado_json[elo_completo] = {
            "kda": round(linha["kda"], 2) if linha["kda"] else 0.0,
            "cs_min": round(linha["cs_min"], 2) if linha["cs_min"] else 0.0,
            "ouro_min": round(linha["ouro_min"], 2) if linha["ouro_min"] else 0.0,
            "visao_min": round(linha["visao_min"], 2) if linha["visao_min"] else 0.0
        }

    with open("cache_benchmarks.json", "w") as f:
        json.dump(resultado_json, f, indent=4)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Cache de Benchmarks Base atualizado!")

def atualizar_cache_benchmarks_rota(conn):
    """
    Agrega os benchmarks por elo + divisao + POSICAO, com todas as metricas
    relevantes a cada rota. Permite avaliar o jogador (e os elos) de forma
    personalizada por funcao no mapa.
    """
    cursor = conn.cursor()
    query = """
        SELECT elo, divisao, posicao, COUNT(*) AS amostra,
               AVG(kda) AS kda, AVG(cs_min) AS cs_min, AVG(ouro_min) AS ouro_min,
               AVG(visao_min) AS visao_min, AVG(dano_min) AS dano_min,
               AVG(dano_objetivos) AS dano_objetivos, AVG(dano_torres) AS dano_torres,
               AVG(tempo_cc) AS tempo_cc, AVG(pink_wards) AS pink_wards,
               AVG(cura_total) AS cura_total, AVG(dano_mitigado) AS dano_mitigado,
               AVG(kpa) AS kpa, AVG(solo_kills) AS solo_kills,
               AVG(cs_jungle_10m) AS cs_jungle_10m, AVG(cs_rota_10m) AS cs_rota_10m,
               AVG(pct_dano_time) AS pct_dano_time
        FROM estatisticas_meta
        WHERE elo IS NOT NULL AND divisao IS NOT NULL
          AND posicao IS NOT NULL AND posicao <> ''
        GROUP BY elo, divisao, posicao
        HAVING COUNT(*) >= 20
    """
    cursor.execute(query)
    linhas = cursor.fetchall()

    # Colunas numericas a expor (mesma uniao usada pelo frontend por rota)
    metricas = [
        "kda", "cs_min", "ouro_min", "visao_min", "dano_min", "dano_objetivos",
        "dano_torres", "tempo_cc", "pink_wards", "cura_total", "dano_mitigado",
        "kpa", "solo_kills", "cs_jungle_10m", "cs_rota_10m", "pct_dano_time",
    ]

    resultado_json = {}
    for linha in linhas:
        elo_completo = f"{linha['elo']}_{linha['divisao']}".upper()
        posicao = str(linha["posicao"]).upper()

        bloco = {"amostra": linha["amostra"]}
        for m in metricas:
            valor = linha[m]
            bloco[m] = round(valor, 4) if valor is not None else 0.0

        resultado_json.setdefault(elo_completo, {})[posicao] = bloco

    with open("cache_benchmarks_rota.json", "w") as f:
        json.dump(resultado_json, f, indent=4)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Cache de Benchmarks por Rota atualizado!")

def atualizar_cache_panorama(conn, mapa_itens_finais):
    """Processa os Top 10 Campeões e a Build ideal apenas com itens completos."""
    cursor = conn.cursor()
    elos = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]
    posicoes = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
    
    panorama_global = {}

    for elo in elos:
        panorama_elo = {}
        for posicao in posicoes:
            query_campeoes = """
                SELECT campeao, COUNT(*) as total_partidas, SUM(vitoria) * 100.0 / COUNT(*) as winrate
                FROM estatisticas_meta
                WHERE UPPER(elo) = ? AND UPPER(posicao) = ?
                GROUP BY campeao
                HAVING total_partidas > 50
                ORDER BY winrate DESC
                LIMIT 10
            """
            cursor.execute(query_campeoes, (elo, posicao))
            campeoes_db = cursor.fetchall()
            
            top_10 = []
            for champ_row in campeoes_db:
                campeao, amostra, winrate = champ_row["campeao"], champ_row["total_partidas"], champ_row["winrate"]
                
                query_itens = "SELECT itens FROM estatisticas_meta WHERE UPPER(elo) = ? AND UPPER(posicao) = ? AND campeao = ?"
                cursor.execute(query_itens, (elo, posicao, campeao))
                linhas_itens = cursor.fetchall()
                
                contador = Counter()
                for linha in linhas_itens:
                    itens_str = linha["itens"]
                    if not itens_str: continue
                    
                    lista_ids = json.loads(itens_str) if "[" in itens_str else itens_str.split(',')
                    
                    for item_id in lista_ids:
                        item_id = str(item_id).strip()
                        
                        # A MÁGICA ACONTECE AQUI:
                        # Só adicionamos na contagem se ele for um Item Final validado!
                        if item_id in mapa_itens_finais:
                            contador[item_id] += 1
                
                top_5_ids = [item_id for item_id, freq in contador.most_common(5)]
                top_5_nomes = [mapa_itens_finais[i] for i in top_5_ids]

                top_10.append({
                    "campeao": campeao,
                    "winrate": round(winrate, 2),
                    "amostra": amostra,
                    "top_5_itens": top_5_nomes
                })
            
            panorama_elo[posicao] = top_10
        panorama_global[elo] = panorama_elo

    with open("cache_panorama.json", "w") as f:
        json.dump(panorama_global, f, indent=4)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Cache do Panorama Meta atualizado!")

def iniciar_agregacao():
    while True:
        try:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Iniciando processamento de caches...")
            conn = sqlite3.connect("meu_meta_dataset_global.db", timeout=20.0)
            conn.row_factory = sqlite3.Row
            
            mapa_itens_finais = obter_mapa_de_itens_finais()
            
            atualizar_cache_benchmarks(conn)
            atualizar_cache_benchmarks_rota(conn)
            atualizar_cache_panorama(conn, mapa_itens_finais)
            
            conn.close()
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Erro crítico no worker: {e}")
            
        time.sleep(3600)

if __name__ == "__main__":
    iniciar_agregacao()
