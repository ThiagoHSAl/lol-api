import os
import time
import requests
import sqlite3
import random
from dotenv import load_dotenv

# ==========================================
# 1. CARREGAMENTO INICIAL DA CHAVE
# ==========================================
load_dotenv()
RAW_KEY = os.getenv("RIOT_API_KEY2")
RIOT_API_KEY = RAW_KEY.replace('"', '').replace("'", "").strip() if RAW_KEY else None

if not RIOT_API_KEY:
    print("❌ Erro FATAL: RIOT_API_KEY não encontrada no .env inicial.")
    exit()

# ==========================================
# 2. CONFIGURAÇÕES GLOBAIS
# ==========================================
REGIOES = {
    "br1": "americas",  # LTA Sul
    "na1": "americas",  # LCS
    "euw1": "europe",   # LEC
    "kr": "asia"        # LCK
}

ELOS = ['IRON', 'BRONZE', 'SILVER', 'GOLD', 'PLATINUM', 'EMERALD', 'DIAMOND']
DIVISOES = ['IV', 'III', 'II', 'I']
PARTIDAS_POR_SUBELO = 50 

# ==========================================
# 3. BANCO DE DADOS (EXPANSÃO MÁXIMA)
# ==========================================
def configurar_banco():
    db_name = 'meu_meta_dataset_global.db'
    # Adicionamos um timeout de 20 segundos (ensina o crawler a esperar na fila)
    conn = sqlite3.connect("meu_meta_dataset_global.db", timeout=20.0)

    # Ativa o modo WAL (Permite que a API leia enquanto o Crawler escreve!)
    conn.execute('PRAGMA journal_mode=WAL;')
    cursor = conn.cursor()
    
    cursor.execute('CREATE TABLE IF NOT EXISTS partidas_processadas (match_id TEXT PRIMARY KEY)')
    
    # Criação da super tabela com todas as métricas isoladas
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS estatisticas_meta (
            id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT, regiao TEXT, elo TEXT, divisao TEXT,
            campeao TEXT, posicao TEXT, vitoria INTEGER, 
            
            -- Métricas Base
            kda REAL, cs_min REAL, ouro_min REAL, visao_min REAL, dano_min REAL, itens TEXT,
            
            -- Macro Gaming
            dano_objetivos REAL, dano_torres REAL, tempo_cc REAL, pink_wards INTEGER,
            
            -- Micro Gaming & Sobrevivência
            cura_total REAL, dano_mitigado REAL, tempo_vivo REAL, first_blood INTEGER, fb_assist INTEGER,
            
            -- Comunicação (Pings)
            pings_perigo INTEGER, pings_ajuda INTEGER, pings_mia INTEGER,
            
            -- Challenges (Métricas Avançadas)
            kpa REAL, skillshots_desviadas INTEGER, solo_kills INTEGER, 
            cs_jungle_10m REAL, cs_rota_10m REAL, pct_dano_time REAL
        )
    ''')
    conn.commit()
    return conn

# ==========================================
# 4. MOTOR DE REQUISIÇÕES (HOT RELOAD)
# ==========================================
def chamada_api(url):
    global RIOT_API_KEY
    headers = {"X-Riot-Token": RIOT_API_KEY}
    
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code == 200: return res.json()
        
        if res.status_code in [401, 403]:
            print(f"\n❌ CHAVE EXPIRADA (Erro {res.status_code})!")
            print("⏳ Cole a nova chave no arquivo .env e salve.")
            print("⏳ O Crawler verificará o arquivo novamente em 60 segundos...")
            time.sleep(60)
            
            load_dotenv(override=True) 
            novo_raw = os.getenv("RIOT_API_KEY")
            RIOT_API_KEY = novo_raw.replace('"', '').replace("'", "").strip() if novo_raw else None
            
            print("🔄 Nova chave detectada! Retomando coleta...")
            return chamada_api(url)
            
        if res.status_code == 429:
            wait = int(res.headers.get('Retry-After', 10))
            print(f"   ⏳ Rate Limit! Pausa de {wait}s...")
            time.sleep(wait + 1)
            return chamada_api(url) 
            
        return None
    except Exception as e:
        print(f"   ⚠️ Erro de rede: {e}")
        return None

# ==========================================
# 5. LOOP DE COLETA (Extração de Alta Densidade)
# ==========================================
def iniciar_crawler():
    conn = configurar_banco()
    cursor = conn.cursor()
    ciclo = 1
    
    try:
        while True:
            print(f"\n🚀 =======================================")
            print(f"🚀 INICIANDO CICLO CONTÍNUO Nº {ciclo}")
            print(f"🚀 =======================================")
            
            for servidor, macro_regiao in REGIOES.items():
                print(f"\n🌍 VARREDURA NO SERVIDOR: {servidor.upper()}")
                
                for elo in ELOS:
                    for div in DIVISOES:
                        print(f"\n🌟 [{servidor.upper()}] Buscando novas partidas: {elo} {div}")
                        partidas = 0
                        
                        url_league = f"https://{servidor}.api.riotgames.com/lol/league/v4/entries/RANKED_SOLO_5x5/{elo}/{div}?page=1"
                        jogadores = chamada_api(url_league)
                        
                        if not jogadores or not isinstance(jogadores, list):
                            continue
                        
                        random.shuffle(jogadores) 
                        
                        for j in jogadores:
                            if partidas >= PARTIDAS_POR_SUBELO: break
                            
                            puuid = j.get('puuid')
                            if not puuid: continue
                            
                            match_ids = chamada_api(f"https://{macro_regiao}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?queue=420&start=0&count=5")
                            if not match_ids: continue
                            
                            for m_id in match_ids:
                                if partidas >= PARTIDAS_POR_SUBELO: break
                                
                                if cursor.execute("SELECT 1 FROM partidas_processadas WHERE match_id = ?", (m_id,)).fetchone(): continue
                                
                                data = chamada_api(f"https://{macro_regiao}.api.riotgames.com/lol/match/v5/matches/{m_id}")
                                if not data or data['info'].get('gameDuration', 0) < 300: continue 
                                
                                duracao_min = data['info']['gameDuration'] / 60.0
                                
                                for p in data['info']['participants']:
                                    
                                    # 1. Métricas Base
                                    kda = (p['kills'] + p['assists']) / max(1, p['deaths'])
                                    cs = p.get('totalMinionsKilled', 0) + p.get('neutralMinionsKilled', 0)
                                    dano = p.get('totalDamageDealtToChampions', 0)
                                    
                                    itens_lista = [str(p.get(f'item{i}', 0)) for i in range(7)]
                                    itens_str = ",".join(itens_lista)

                                    # 2. Dicionário de Challenges (Onde mora o ouro)
                                    chal = p.get('challenges', {})
                                    
                                    # 3. Extração Segura de Dados (.get impede o script de quebrar se a Riot esquecer uma chave)
                                    cursor.execute('''
                                        INSERT INTO estatisticas_meta (
                                            match_id, regiao, elo, divisao, campeao, posicao, vitoria, 
                                            kda, cs_min, ouro_min, visao_min, dano_min, itens,
                                            dano_objetivos, dano_torres, tempo_cc, pink_wards,
                                            cura_total, dano_mitigado, tempo_vivo, first_blood, fb_assist,
                                            pings_perigo, pings_ajuda, pings_mia,
                                            kpa, skillshots_desviadas, solo_kills, cs_jungle_10m, cs_rota_10m, pct_dano_time
                                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    ''', (
                                        m_id, servidor, elo, div, p.get('championName'), p.get('teamPosition'), 1 if p.get('win') else 0,
                                        # Base
                                        kda, cs / duracao_min, p.get('goldEarned', 0) / duracao_min, p.get('visionScore', 0) / duracao_min, dano / duracao_min, itens_str,
                                        # Macro
                                        p.get('damageDealtToObjectives', 0), p.get('damageDealtToBuildings', 0), p.get('timeCCingOthers', 0), p.get('visionWardsBoughtInGame', 0),
                                        # Micro
                                        p.get('totalHeal', 0), p.get('damageSelfMitigated', 0), p.get('longestTimeSpentLiving', 0), 1 if p.get('firstBloodKill') else 0, 1 if p.get('firstBloodAssist') else 0,
                                        # Pings (Somando o Amarelo de Recuar com o Vermelho de Perigo)
                                        (p.get('getBackPings', 0) + p.get('dangerPings', 0)), p.get('assistMePings', 0), p.get('enemyMissingPings', 0),                                        chal.get('kpa', 0), chal.get('skillshotsDodged', 0), chal.get('soloKills', 0), chal.get('jungleCsBefore10Minutes', 0), chal.get('laneMinionsFirst10Minutes', 0), chal.get('teamDamagePercentage', 0)
                                    ))
                                
                                cursor.execute("INSERT INTO partidas_processadas VALUES (?)", (m_id,))
                                conn.commit()
                                partidas += 1
                                print(f"   ✅ [{servidor.upper()} - {elo} {div}] Partidas Coletadas: {partidas}/{PARTIDAS_POR_SUBELO}")
                                time.sleep(1.2)
            
            print(f"\n✅ Ciclo {ciclo} concluído em todas as regiões!")
            ciclo += 1
            print("⏳ Pausa de 10 segundos antes de reiniciar a varredura global...")
            time.sleep(10)
            
    except KeyboardInterrupt:
        print("\n\n🛑 Comando de Interrupção (Ctrl+C) recebido do teclado!")
        print("🛑 Finalizando as operações pendentes com segurança...")
    finally:
        conn.close()
        print("💾 Conexão com o banco de dados encerrada. Dados salvos com sucesso!")

if __name__ == "__main__":
    iniciar_crawler()
