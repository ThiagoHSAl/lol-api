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
KEY_NAME = "RIOT_API_KEY2"  # este crawler usa SEMPRE esta chave (boot e hot-reload)
RAW_KEY = os.getenv(KEY_NAME)
RIOT_API_KEY = RAW_KEY.replace('"', '').replace("'", "").strip() if RAW_KEY else None

INTERVALO = 1.25       # s entre QUALQUER requisição (dev: 100/2min = 1.2s + folga p/ jitter)
_ultimo_req = 0.0


def _aguardar_pace():
    """Garante >= INTERVALO s desde a requisição anterior. Paceia TODA chamada (não só as
    que viram partida salva) — é o que evita estourar o limite da chave."""
    global _ultimo_req
    espera = _ultimo_req + INTERVALO - time.time()
    if espera > 0:
        time.sleep(espera)
    _ultimo_req = time.time()

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
    conn = sqlite3.connect("meu_meta_dataset_global.db", timeout=120.0)

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
    # Índice para as consultas por campeão (benchmark por-campeão/pool). PRECISA ser um
    # índice de EXPRESSÃO sobre UPPER(campeao)/UPPER(posicao) porque a query filtra com
    # UPPER(...) (case-insensitive) — um índice nas colunas cruas NÃO é usado e o AVG
    # cai em varredura de tabela inteira (~19s). Inclui elo, divisao p/ o GROUP BY usar
    # a ordem do índice (sem b-tree temporário).
    # Nome próprio (idx_camp_pos_upper) para não colidir com um eventual idx_campeao_upper
    # single-column pré-existente — IF NOT EXISTS sobre nome ocupado seria no-op silencioso.
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_camp_pos_upper
        ON estatisticas_meta(UPPER(campeao), UPPER(posicao), elo, divisao)
    ''')
    garantir_colunas(conn)  # colunas de sinais crus (puuid, individual_position, ...)
    conn.commit()
    return conn

# ==========================================
# 4. MOTOR DE REQUISIÇÕES (HOT RELOAD)
# ==========================================
def chamada_api(url):
    global RIOT_API_KEY
    tentativas_429 = 0
    while True:
        _aguardar_pace()  # pace por REQUISIÇÃO (toda chamada, não só as bem-sucedidas)
        try:
            res = requests.get(url, headers={"X-Riot-Token": RIOT_API_KEY}, timeout=15)
        except Exception as e:
            print(f"   ⚠️ Erro de rede: {e}")
            return None

        sc = res.status_code
        if sc == 200:
            return res.json()

        if sc in (401, 403):
            # Hot-reload SEM recursão: espera até aparecer uma chave NOVA no .env
            # (mesma KEY_NAME do boot — não troca de chave por engano).
            print(f"\n❌ CHAVE EXPIRADA (Erro {sc})! Cole a nova em {KEY_NAME} no .env e salve.")
            atual = RIOT_API_KEY
            while True:
                time.sleep(60)
                load_dotenv(override=True)
                novo_raw = os.getenv(KEY_NAME)
                novo = novo_raw.replace('"', '').replace("'", "").strip() if novo_raw else None
                if novo and novo != atual:
                    RIOT_API_KEY = novo
                    print("🔄 Nova chave detectada! Retomando coleta...")
                    break
                print("⏳ Ainda sem chave nova; verifico de novo em 60s...")
            continue

        if sc == 429:
            tentativas_429 += 1
            wait = min(int(res.headers.get("Retry-After", 5)) + tentativas_429, 30)
            print(f"   ⏳ Rate Limit (429)! Pausa de {wait}s...")
            time.sleep(wait)
            continue

        return None  # 4xx/5xx não tratados

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

                                # Cruza os sinais da match-v5, grava a ROTA inferida (não o
                                # teamPosition cru), pula participantes com rota indeduzível e
                                # persiste os sinais crus p/ re-derivação futura sem re-fetch.
                                inserir_partida(cursor, data, m_id, servidor, elo, div)

                                cursor.execute("INSERT INTO partidas_processadas VALUES (?)", (m_id,))
                                conn.commit()
                                partidas += 1
                                print(f"   ✅ [{servidor.upper()} - {elo} {div}] Partidas Coletadas: {partidas}/{PARTIDAS_POR_SUBELO}")
                                # pace por requisição agora é central (_aguardar_pace em chamada_api)
            
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
