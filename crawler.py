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
# Este crawler (IRON→DIAMOND) usa a DEV KEY 1. A personal key é EXCLUSIVA do app
# EloRise; cada crawler tem sua própria dev key (orçamentos independentes de 100/2min).
# As dev keys expiram a cada 24h — o hot-reload abaixo (401/403) espera a nova em
# RIOT_DEV_KEY no .env. Pacer com arquivo de estado PRÓPRIO (.pacer_dev1) para não
# competir com o crawler apex, que roda na outra chave.
load_dotenv()
KEY_NAME = "RIOT_DEV_KEY"  # dev key 1 (boot e hot-reload leem a mesma)
RAW_KEY = os.getenv(KEY_NAME)
RIOT_API_KEY = RAW_KEY.replace('"', '').replace("'", "").strip() if RAW_KEY else None

# fator 0.9: a dev key é dedicada a este crawler (não dividida com o app), então
# reservamos só uma folga pequena. Arquivo por chave — pacing independente do apex.
PACER = RiotPacer(
    arquivo=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pacer_dev1.json"),
    fator_uso=0.9,
)

# Filas coletadas e o queueId da Riot correspondente. A rota/benchmark do app usa só
# 'solo'; flex e normal entram no mesmo DB separados pela coluna `fila`.
# Quais filas coletar é configurável por CRAWLER_FILAS no .env (csv). Default = as 3.
# Ex.: CRAWLER_FILAS=flex,normal foca a coleta nas filas rala (solo já tem 5M linhas).
_QUEUE_POR_FILA = {"solo": 420, "flex": 440, "normal": 400}
FILAS = {f: _QUEUE_POR_FILA[f] for f in
         os.getenv("CRAWLER_FILAS", "solo,flex,normal").split(",")
         if f.strip() in _QUEUE_POR_FILA}

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

# Meta MENOR em normal/flex: cada partida dessas filas custa ~10 lookups de rank
# (league-v4/by-puuid) além de match+timeline. Coletar menos por sub-elo mantém o
# gasto de API sob controle. Configurável por META_FLEX / META_NORMAL no .env.
META_POR_FILA = {
    "solo": PARTIDAS_POR_SUBELO,
    "flex": int(os.getenv("META_FLEX", "25")),
    "normal": int(os.getenv("META_NORMAL", "25")),
}

# Cache PERSISTENTE de rank por jogador (arquivo próprio da dev key 1).
RANKS = RankCache(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ranks_dev1.json"))

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
                        # Lista de jogadores do sub-elo é buscada UMA vez e reusada nas 3 filas.
                        url_league = f"https://{servidor}.api.riotgames.com/lol/league/v4/entries/RANKED_SOLO_5x5/{elo}/{div}?page=1"
                        jogadores = chamada_api(url_league)

                        if not jogadores or not isinstance(jogadores, list):
                            continue

                        random.shuffle(jogadores)

                        for fila, queue_id in FILAS.items():
                            meta_fila = META_POR_FILA.get(fila, PARTIDAS_POR_SUBELO)
                            print(f"\n🌟 [{servidor.upper()}] {elo} {div} | fila={fila} (Meta: {meta_fila})")
                            partidas = 0

                            for j in jogadores:
                                if partidas >= meta_fila: break

                                puuid = j.get('puuid')
                                if not puuid: continue

                                match_ids = chamada_api(f"https://{macro_regiao}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?queue={queue_id}&start=0&count=5")
                                if not match_ids: continue

                                for m_id in match_ids:
                                    if partidas >= meta_fila: break

                                    if cursor.execute("SELECT 1 FROM partidas_processadas WHERE match_id = ?", (m_id,)).fetchone(): continue

                                    data = chamada_api(f"https://{macro_regiao}.api.riotgames.com/lol/match/v5/matches/{m_id}")
                                    if not data or data['info'].get('gameDuration', 0) < 300: continue

                                    # Timeline só APÓS o filtro de duração (evita gastar chamada em
                                    # partida curta descartada). Dela saem as botas realmente compradas.
                                    timeline = chamada_api(f"https://{macro_regiao}.api.riotgames.com/lol/match/v5/matches/{m_id}/timeline")

                                    # Normal/flex: elo REAL de cada jogador (lookup por puuid, cacheado);
                                    # solo: mapa=None → mantém o rótulo do semente.
                                    mapa_ranks = construir_mapa_ranks(data, servidor, fila, chamada_api, RANKS)

                                    # Rota inferida + sinais crus + fila + botas da timeline.
                                    inserir_partida(cursor, data, m_id, servidor, elo, div, fila=fila,
                                                    timeline=timeline, elo_por_puuid=mapa_ranks)

                                    cursor.execute("INSERT INTO partidas_processadas VALUES (?)", (m_id,))
                                    conn.commit()
                                    partidas += 1
                                    print(f"   ✅ [{servidor.upper()} - {elo} {div} {fila}] Coletadas: {partidas}/{meta_fila}")
                                    # pace por requisição é central (PACER.aguardar em chamada_api)
            
            print(f"\n✅ Ciclo {ciclo} concluído em todas as regiões!")
            ciclo += 1
            print("⏳ Pausa de 10 segundos antes de reiniciar a varredura global...")
            time.sleep(10)
            
    except KeyboardInterrupt:
        print("\n\n🛑 Comando de Interrupção (Ctrl+C) recebido do teclado!")
        print("🛑 Finalizando as operações pendentes com segurança...")
    finally:
        RANKS.salvar()  # persiste o cache de ranks acumulado
        conn.close()
        print("💾 Conexão com o banco de dados encerrada. Dados salvos com sucesso!")

if __name__ == "__main__":
    iniciar_crawler()
