import os
import time
import requests
import sqlite3
import random
from dotenv import load_dotenv
from ingest_crawler import garantir_colunas, inserir_partida  # rota inferida + sinais crus
from riot_pacer import RiotPacer
from ranks import RankCache, construir_mapa_ranks  # elo por jogador na normal/flex

# ==========================================
# 1. CARREGAMENTO INICIAL DA CHAVE
# ==========================================
# Este crawler (MASTER/GM/CHALLENGER) usa a DEV KEY 2 — chave própria, orçamento
# independente do crawler.py. A personal key é EXCLUSIVA do app EloRise. Dev keys
# expiram a cada 24h; o hot-reload (401/403) espera a nova em RIOT_DEV_KEY2 no .env.
load_dotenv()
KEY_NAME = "RIOT_DEV_KEY2"  # dev key 2 (boot e hot-reload leem a mesma)
RAW_KEY = os.getenv(KEY_NAME)
RIOT_API_KEY = RAW_KEY.replace('"', '').replace("'", "").strip() if RAW_KEY else None

# Arquivo de estado PRÓPRIO (.pacer_dev2): pacing independente do crawler.py, que
# roda na outra dev key. fator 0.9 (chave dedicada, só pequena folga).
PACER = RiotPacer(
    arquivo=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pacer_dev2.json"),
    fator_uso=0.9,
)

# Filas coletadas e o queueId da Riot. Só 'solo' alimenta rota/benchmark do app;
# flex e normal ficam no mesmo DB, separados pela coluna `fila`.
# Quais filas coletar é configurável por CRAWLER_FILAS no .env (csv). Default = as 3.
_QUEUE_POR_FILA = {"solo": 420, "flex": 440, "normal": 400}
FILAS = {f: _QUEUE_POR_FILA[f] for f in
         os.getenv("CRAWLER_FILAS", "solo,flex,normal").split(",")
         if f.strip() in _QUEUE_POR_FILA}

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

# Meta MENOR em normal/flex: cada partida dessas filas custa ~10 lookups de rank
# (league-v4/by-puuid) além de match+timeline. Configurável por META_FLEX / META_NORMAL.
META_POR_FILA = {
    "solo": META_PARTIDAS_POR_ELO,
    "flex": int(os.getenv("META_FLEX", "100")),
    "normal": int(os.getenv("META_NORMAL", "100")),
}

# Cache PERSISTENTE de rank por jogador (arquivo próprio da dev key 2).
RANKS = RankCache(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ranks_dev2.json"))

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
    tentativas_429 = 0
    while True:
        PACER.aguardar()  # slot global por REQUISIÇÃO — compartilhado entre processos
        try:
            res = requests.get(url, headers={"X-Riot-Token": RIOT_API_KEY}, timeout=15)
        except Exception as e:
            print(f"   ⚠️ Erro de rede: {e}")
            return None

        PACER.observar(res.headers)  # ajusta o ritmo pelo X-App-Rate-Limit real
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
                if not (novo and novo != atual):
                    print("⏳ Ainda sem chave nova; verifico de novo em 60s...")
                    continue
                RIOT_API_KEY = novo
                # Chaves dev da Riot levam alguns segundos p/ propagar: uma nova
                # pode dar 401 logo após ser gerada. Revalidamos a MESMA chave
                # algumas vezes antes de desistir — senão descartaríamos uma chave
                # boa e ficaríamos presos esperando outra que nunca vem.
                print("🔄 Nova chave detectada! Revalidando (propagação)...")
                for tentativa in range(1, 7):  # ~90s de margem
                    time.sleep(15)
                    try:
                        teste = requests.get(url, headers={"X-Riot-Token": RIOT_API_KEY}, timeout=15).status_code
                    except Exception:
                        continue
                    if teste not in (401, 403):
                        print("✅ Chave validada! Retomando coleta...")
                        break
                    print(f"   ⏳ Chave ainda propagando ({teste}); tentativa {tentativa}/6...")
                else:
                    # Não validou em ~90s: trata como ruim e volta a esperar outra
                    # (atual = novo evita reprocessar o mesmo valor em loop).
                    atual = novo
                    print("⚠️ Chave colada não validou; aguardando outra no .env...")
                    continue
                break
            continue

        if sc == 429:
            tentativas_429 += 1
            wait = min(int(res.headers.get("Retry-After", 5)) + tentativas_429, 30)
            print(f"   ⏳ Rate Limit (429)! Pausa GLOBAL de {wait}s...")
            PACER.penalizar(wait)  # empurra o slot de TODOS os processos, não só este
            continue

        return None  # 4xx/5xx não tratados

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
                    # Liga apex é buscada UMA vez por elo e reusada nas 3 filas.
                    url_liga = f"https://{servidor}.api.riotgames.com/lol/league/v4/{endpoint}/by-queue/RANKED_SOLO_5x5"
                    resposta = chamada_api(url_liga)

                    # Nos elos Apex, a Riot devolve um Dicionário. Os jogadores estão na chave 'entries'
                    jogadores = resposta.get('entries', []) if isinstance(resposta, dict) else []

                    if not jogadores:
                        print(f"   ⚠️ Nenhum jogador encontrado em {elo}. Pulando...")
                        continue

                    random.shuffle(jogadores)

                    for fila, queue_id in FILAS.items():
                        meta_fila = META_POR_FILA.get(fila, META_PARTIDAS_POR_ELO)
                        print(f"\n🌟 [{servidor.upper()}] {elo} | fila={fila} (Meta: {meta_fila})")
                        partidas_coletadas = 0

                        for j in jogadores:
                            if partidas_coletadas >= meta_fila: break

                            puuid = j.get('puuid')
                            if not puuid: continue

                            match_ids = chamada_api(f"https://{macro_regiao}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?queue={queue_id}&start=0&count=5")
                            if not match_ids: continue

                            for m_id in match_ids:
                                if partidas_coletadas >= meta_fila: break

                                if cursor.execute("SELECT 1 FROM partidas_processadas WHERE match_id = ?", (m_id,)).fetchone(): continue

                                data = chamada_api(f"https://{macro_regiao}.api.riotgames.com/lol/match/v5/matches/{m_id}")
                                if not data or data['info'].get('gameDuration', 0) < 300: continue

                                # Timeline só APÓS o filtro de duração — dela saem as botas compradas.
                                timeline = chamada_api(f"https://{macro_regiao}.api.riotgames.com/lol/match/v5/matches/{m_id}/timeline")

                                # Normal/flex: elo REAL de cada jogador (lookup por puuid, cacheado);
                                # solo: mapa=None → divisão padrão "I" do semente apex.
                                mapa_ranks = construir_mapa_ranks(data, servidor, fila, chamada_api, RANKS)

                                # Rota inferida + sinais crus + fila + botas; divisão padrão "I" no apex.
                                inserir_partida(cursor, data, m_id, servidor, elo, "I", fila=fila,
                                                timeline=timeline, elo_por_puuid=mapa_ranks)

                                cursor.execute("INSERT INTO partidas_processadas VALUES (?)", (m_id,))
                                conn.commit()
                                partidas_coletadas += 1
                                print(f"   ✅ [{servidor.upper()} - {elo} {fila}] Coletadas: {partidas_coletadas}/{meta_fila}")
                                # pace por requisição é central (PACER.aguardar em chamada_api)
            
            print(f"\n✅ Ciclo {ciclo} High Elo concluído!")
            ciclo += 1
            print("⏳ Pausa de 10 segundos antes de reiniciar a varredura Apex...")
            time.sleep(10)
            
    except KeyboardInterrupt:
        print("\n\n🛑 Comando de Interrupção (Ctrl+C) recebido do teclado!")
        print("🛑 Finalizando as operações pendentes com segurança...")
    finally:
        RANKS.salvar()  # persiste o cache de ranks acumulado
        conn.close()
        print("💾 Conexão com o banco de dados encerrada. Dados salvos com sucesso!")

if __name__ == "__main__":
    iniciar_crawler_apex()
