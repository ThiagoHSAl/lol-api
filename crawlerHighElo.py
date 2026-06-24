import os
import time
import requests
import sqlite3
import random
from dotenv import load_dotenv
from ingest_crawler import garantir_colunas, inserir_partida  # rota inferida + sinais crus

# ==========================================
# 1. CARREGAMENTO INICIAL DA CHAVE
# ==========================================
load_dotenv()
RAW_KEY = os.getenv("RIOT_API_KEY")
RIOT_API_KEY = RAW_KEY.replace('"', '').replace("'", "").strip() if RAW_KEY else None

if not RIOT_API_KEY:
    print("❌ Erro FATAL: RIOT_API_KEY não encontrada no .env inicial.")
    exit()

# ==========================================
# 2. CONFIGURAÇÕES FOCADAS EM HIGH ELO
# ==========================================
REGIOES = {
    "br1": "americas",  # LTA Sul
    "na1": "americas",  # LCS
    "euw1": "europe",   # LEC
    "kr": "asia"        # LCK
}

# Dicionário mapeando o Nome do Elo para o Endpoint exato da Riot API
ELOS_APEX = {
    "MASTER": "masterleagues",
    "GRANDMASTER": "grandmasterleagues",
    "CHALLENGER": "challengerleagues"
}

# ⚠️ VOLUME ALTO: Como o objetivo é equalizar o dataset, vamos puxar mais partidas por ciclo
META_PARTIDAS_POR_ELO = 200 

# ==========================================
# 3. CONEXÃO COM O BANCO DE DADOS GLOBAL
# ==========================================
def configurar_banco():
    conn = sqlite3.connect("meu_meta_dataset_global.db", timeout=120.0)
    # Fundamental: WAL mode ativado para permitir gravação simultânea com outros scripts
    conn.execute('PRAGMA journal_mode=WAL;')
    
    # Garantimos que as tabelas existem (caso rode este script em outro ambiente)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS partidas_processadas (match_id TEXT PRIMARY KEY)')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS estatisticas_meta (
            id INTEGER PRIMARY KEY AUTOINCREMENT, match_id TEXT, regiao TEXT, elo TEXT, divisao TEXT,
            campeao TEXT, posicao TEXT, vitoria INTEGER,
            kda REAL, cs_min REAL, ouro_min REAL, visao_min REAL, dano_min REAL, itens TEXT,
            dano_objetivos REAL, dano_torres REAL, tempo_cc REAL, pink_wards INTEGER,
            cura_total REAL, dano_mitigado REAL, tempo_vivo REAL, first_blood INTEGER, fb_assist INTEGER,
            pings_perigo INTEGER, pings_ajuda INTEGER, pings_mia INTEGER,
            kpa REAL, skillshots_desviadas INTEGER, solo_kills INTEGER, cs_jungle_10m REAL, cs_rota_10m REAL, pct_dano_time REAL
        )
    ''')
    garantir_colunas(conn)  # colunas de sinais crus (puuid, individual_position, ...)
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
            print("⏳ Atualize o arquivo .env com a nova chave.")
            print("⏳ O Crawler Apex verificará novamente em 60 segundos...")
            time.sleep(60)
            
            load_dotenv(override=True) 
            novo_raw = os.getenv("RIOT_API_KEY")
            RIOT_API_KEY = novo_raw.replace('"', '').replace("'", "").strip() if novo_raw else None
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
# 5. LOOP DE COLETA EXCLUSIVO APEX
# ==========================================
def iniciar_crawler_apex():
    conn = configurar_banco()
    cursor = conn.cursor()
    ciclo = 1
    
    try:
        while True:
            print(f"\n👑 =======================================")
            print(f"👑 INICIANDO CICLO HIGH ELO Nº {ciclo}")
            print(f"👑 =======================================")
            
            for servidor, macro_regiao in REGIOES.items():
                print(f"\n🌍 VARREDURA APEX NO SERVIDOR: {servidor.upper()}")
                
                for elo, endpoint in ELOS_APEX.items():
                    print(f"\n🌟 [{servidor.upper()}] Buscando a elite: {elo} (Meta: {META_PARTIDAS_POR_ELO} partidas)")
                    partidas_coletadas = 0
                    
                    # Rota específica da Riot para Ligas Apex
                    url_liga = f"https://{servidor}.api.riotgames.com/lol/league/v4/{endpoint}/by-queue/RANKED_SOLO_5x5"
                    resposta = chamada_api(url_liga)
                    
                    # Nos elos Apex, a Riot devolve um Dicionário. Os jogadores estão na chave 'entries'
                    jogadores = resposta.get('entries', []) if isinstance(resposta, dict) else []
                    
                    if not jogadores:
                        print(f"   ⚠️ Nenhum jogador encontrado em {elo}. Pulando...")
                        continue
                    
                    random.shuffle(jogadores) 
                    
                    for j in jogadores:
                        if partidas_coletadas >= META_PARTIDAS_POR_ELO: break
                        
                        puuid = j.get('puuid')
                        if not puuid: continue
                        
                        match_ids = chamada_api(f"https://{macro_regiao}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?queue=420&start=0&count=5")
                        if not match_ids: continue
                        
                        for m_id in match_ids:
                            if partidas_coletadas >= META_PARTIDAS_POR_ELO: break
                            
                            if cursor.execute("SELECT 1 FROM partidas_processadas WHERE match_id = ?", (m_id,)).fetchone(): continue
                            
                            data = chamada_api(f"https://{macro_regiao}.api.riotgames.com/lol/match/v5/matches/{m_id}")
                            if not data or data['info'].get('gameDuration', 0) < 300: continue

                            # Rota inferida (não o teamPosition cru) + sinais crus persistidos;
                            # divisão padrão "I" para manter a sanidade do banco de dados.
                            inserir_partida(cursor, data, m_id, servidor, elo, "I")

                            cursor.execute("INSERT INTO partidas_processadas VALUES (?)", (m_id,))
                            conn.commit()
                            partidas_coletadas += 1
                            print(f"   ✅ [{servidor.upper()} - {elo}] Partidas Coletadas: {partidas_coletadas}/{META_PARTIDAS_POR_ELO}")
                            time.sleep(1.2) # Respeita o limite da Riot API
            
            print(f"\n✅ Ciclo {ciclo} High Elo concluído!")
            ciclo += 1
            print("⏳ Pausa de 10 segundos antes de reiniciar a varredura Apex...")
            time.sleep(10)
            
    except KeyboardInterrupt:
        print("\n\n🛑 Comando de Interrupção (Ctrl+C) recebido do teclado!")
        print("🛑 Finalizando as operações pendentes com segurança...")
    finally:
        conn.close()
        print("💾 Conexão com o banco de dados encerrada. Dados salvos com sucesso!")

if __name__ == "__main__":
    iniciar_crawler_apex()
