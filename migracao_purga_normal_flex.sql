-- migracao_purga_normal_flex.sql
-- Purga as linhas de fila NORMAL e FLEX, que estavam com o elo do jogador-SEMENTE
-- carimbado nos 10 participantes (rótulo errado). A partir do novo código dos crawlers,
-- normal/flex são recoletadas com o elo REAL de cada jogador (ver ranks.py); normal ganha
-- ainda o bucket UNRANKED. Não dá p/ corrigir retroativamente (o rank individual da época
-- não foi guardado), então a única forma correta é apagar e recoletar.
--
-- Rodar com os crawlers PARADOS (evita contenção do lock de escrita no WAL):
--   sudo systemctl stop crawler crawler-apex
--   sqlite3 meu_meta_dataset_global.db < migracao_purga_normal_flex.sql
--   sudo systemctl start crawler crawler-apex
--
-- Observação: fila NULL == 'solo' (dados antigos pré-migração), então NÃO é tocada.

BEGIN;

-- Libera os match_ids dessas filas para recoleta (partidas_processadas é o dedup global).
DELETE FROM partidas_processadas
 WHERE match_id IN (SELECT match_id FROM estatisticas_meta WHERE fila IN ('normal', 'flex'));

-- Remove as estatísticas mal rotuladas.
DELETE FROM estatisticas_meta WHERE fila IN ('normal', 'flex');

COMMIT;
