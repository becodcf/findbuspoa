-- Roteiro de demonstração do banco no pgAdmin.
-- Abrir em: banco uberbus > Tools > Query Tool > Open File (este arquivo).
-- Rodar UMA consulta por vez: selecionar o bloco com o mouse e apertar F5.


-- 1. Visão geral: as tabelas do projeto e quantos registros cada uma tem
SELECT 'linhas' AS tabela, count(*) AS registros FROM linhas
UNION ALL SELECT 'paradas', count(*) FROM paradas
UNION ALL SELECT 'linha_paradas', count(*) FROM linha_paradas
UNION ALL SELECT 'horarios', count(*) FROM horarios
UNION ALL SELECT 'itinerarios', count(*) FROM itinerarios
UNION ALL SELECT 'frota', count(*) FROM frota
UNION ALL SELECT 'lugares', count(*) FROM lugares
UNION ALL SELECT 'geocode_cache', count(*) FROM geocode_cache
UNION ALL SELECT 'buscas', count(*) FROM buscas
ORDER BY registros DESC;


-- 2. Uma linha que não existia no GTFS antigo e foi reconstruída: R16 (Restinga)
SELECT numero, nome, tracado FROM linhas WHERE numero = 'R16';

-- 2b. As paradas dela, em ordem, no sentido bairro -> centro
SELECT lp.seq, p.nome, p.lat, p.lon
FROM linha_paradas lp
JOIN paradas p USING (stop_id)
JOIN linhas l USING (route_id)
WHERE l.numero = 'R16' AND lp.sentido = 'BAIRROCENTRO'
ORDER BY lp.seq;

-- 2c. As ruas por onde ela passa, como vieram da EPTC
SELECT seq, logradouro
FROM itinerarios
WHERE linha = 'R16' AND sentido = 'BAIRROCENTRO' AND tipo = 'LOGRADOURO'
ORDER BY seq;


-- 3. Horários: quantas saídas existem em cada tipo de tabela (oficial, verão, carnaval...)
SELECT tipo_tabela, count(*) AS saidas, count(DISTINCT linha) AS linhas
FROM horarios
GROUP BY tipo_tabela
ORDER BY saidas DESC;

-- 3b. Saídas da linha 343 em dia útil, na tabela oficial
SELECT sentido, string_agg(horario, '  ' ORDER BY horario) AS saidas
FROM horarios
WHERE linha = '343' AND tipo_tabela = 'OFICIAL' AND tipo_dia = 'DIAUTIL'
GROUP BY sentido;


-- 4. A busca de lugares: acha "Clínicas" mesmo digitando sem acento
--    (índice de trigramas pg_trgm + função normaliza com unaccent)
SELECT tipo, nome, detalhe,
       round(word_similarity(normaliza('clinicas'), normaliza(nome))::numeric, 2) AS parecido
FROM lugares
WHERE normaliza('clinicas') <% normaliza(nome)
ORDER BY parecido DESC, peso DESC
LIMIT 10;

-- 4b. Endereço com número de casa vindo do OpenStreetMap
SELECT nome, detalhe, lat, lon
FROM lugares
WHERE tipo = 'endereco' AND nome ILIKE '%Ramiro Barcelos, 2350';


-- 5. O uso do sistema: últimas buscas de rota feitas na página
SELECT criado_em, de_nome, para_nome, n_opcoes
FROM buscas
ORDER BY id DESC
LIMIT 10;

-- 5b. Endereços já consultados no Nominatim (guardados para não repetir a consulta)
SELECT consulta, jsonb_array_length(resultado) AS opcoes, criado_em
FROM geocode_cache
ORDER BY criado_em DESC;


-- 6. Curiosidade: a frota por empresa
SELECT operador, count(*) AS onibus, round(avg(ano_fabricacao)) AS ano_medio
FROM frota
GROUP BY operador
ORDER BY onibus DESC;
