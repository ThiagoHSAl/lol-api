import os
import requests
import json
from dotenv import load_dotenv

# ==========================================
# 1. CARREGAMENTO SEGURO DA CHAVE (.env)
# ==========================================
load_dotenv()
RAW_KEY = os.getenv("RIOT_API_KEY")
RIOT_API_KEY = RAW_KEY.replace('"', '').replace("'", "").strip() if RAW_KEY else None

if not RIOT_API_KEY:
    print("❌ Erro FATAL: RIOT_API_KEY não encontrada no arquivo .env.")
    print("Verifique se o arquivo .env está na mesma pasta que este script.")
    exit()

headers = {"X-Riot-Token": RIOT_API_KEY}

elos_apex = {
    "MASTER": "masterleagues",
    "GRANDMASTER": "grandmasterleagues",
    "CHALLENGER": "challengerleagues"
}

print("🔍 Iniciando Simulação de Extração do Crawler (High Elo)...")

for elo_nome, endpoint in elos_apex.items():
    print(f"\n📡 Buscando 1 partida de {elo_nome}...")
    
    # 1. Bate no endpoint da Liga
    url_liga = f"https://br1.api.riotgames.com/lol/league/v4/{endpoint}/by-queue/RANKED_SOLO_5x5"
    resposta_liga = requests.get(url_liga, headers=headers).json()
    
    if 'status' in resposta_liga:
        print(f"❌ Resposta da Riot: {resposta_liga['status']}")
        continue

    jogadores = resposta_liga.get('entries', [])
    if not jogadores:
        print(f"❌ Erro: Lista de jogadores vazia.")
        continue
        
    # Pega o puuid do primeiro jogador
    puuid = jogadores[0].get('puuid')
    
    # 2. Puxa as últimas partidas desse jogador
    url_ids = f"https://americas.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?queue=420&start=0&count=1"
    match_ids = requests.get(url_ids, headers=headers).json()
    
    if not match_ids:
        print(f"❌ Jogador não tem partidas ranqueadas.")
        continue
        
    match_id = match_ids[0]
    
    # 3. Puxa os detalhes da partida
    url_partida = f"https://americas.api.riotgames.com/lol/match/v5/matches/{match_id}"
    dados_partida = requests.get(url_partida, headers=headers).json()
    
    if 'info' not in dados_partida:
        print("❌ Erro ao ler informações da partida.")
        continue

    # ==========================================
    # 4. SIMULAÇÃO EXATA DA LÓGICA DO SEU CRAWLER
    # ==========================================
    duracao_min = dados_partida['info']['gameDuration'] / 60.0
    
    # Pegamos apenas o primeiro jogador da partida para análise
    p = dados_partida['info']['participants'][0]
    
    kda = (p['kills'] + p['assists']) / max(1, p['deaths'])
    cs = p.get('totalMinionsKilled', 0) + p.get('neutralMinionsKilled', 0)
    dano = p.get('totalDamageDealtToChampions', 0)
    itens_lista = [str(p.get(f'item{i}', 0)) for i in range(7)]
    chal = p.get('challenges', {})

    # Monta o dicionário espelhando as colunas do seu banco de dados
    dados_extraidos = {
        "match_id": match_id,
        "elo_simulado": elo_nome,
        "campeao": p.get('championName'),
        "posicao": p.get('teamPosition'),
        "vitoria": 1 if p.get('win') else 0,
        "kda": round(kda, 2),
        "cs_min": round(cs / duracao_min, 2),
        "ouro_min": round(p.get('goldEarned', 0) / duracao_min, 2),
        "visao_min": round(p.get('visionScore', 0) / duracao_min, 2),
        "dano_min": round(dano / duracao_min, 2),
        "itens": ",".join(itens_lista),
        "dano_objetivos": p.get('damageDealtToObjectives', 0),
        "dano_torres": p.get('damageDealtToBuildings', 0),
        "tempo_cc": p.get('timeCCingOthers', 0),
        "pink_wards": p.get('visionWardsBoughtInGame', 0),
        "cura_total": p.get('totalHeal', 0),
        "dano_mitigado": p.get('damageSelfMitigated', 0),
        "tempo_vivo": p.get('longestTimeSpentLiving', 0),
        "first_blood": 1 if p.get('firstBloodKill') else 0,
        "fb_assist": 1 if p.get('firstBloodAssist') else 0,
        "pings_perigo": p.get('dangerPings', 0),
        "pings_ajuda": p.get('assistMePings', 0),
        "pings_mia": p.get('enemyMissingPings', 0),
        "kpa": round(chal.get('killParticipation', 0), 3),
        "skillshots_desviadas": chal.get('skillshotsDodged', 0),
        "solo_kills": chal.get('soloKills', 0),
        "cs_jungle_10m": chal.get('jungleCsBefore10Minutes', 0),
        "cs_rota_10m": chal.get('laneMinionsFirst10Minutes', 0),
        "pct_dano_time": round(chal.get('teamDamagePercentage', 0), 3)
    }
    
    # Salva APENAS o dicionário final (o que iria para o SQLite)
    nome_arquivo = f"dados_banco_{elo_nome.lower()}.json"
    with open(nome_arquivo, "w") as f:
        json.dump(dados_extraidos, f, indent=4)
        
    print(f"✅ Extração validada e salva: {nome_arquivo}")

print("\n🚀 Teste de integridade concluído! Abra os arquivos 'dados_banco_...' para verificar.")
