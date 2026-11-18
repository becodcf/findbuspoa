-- Esquema do banco do FindBus POA (PostgreSQL 14+). O banco e o usuário continuam se chamando uberbus.
-- Recriado do zero a cada carga por etl/carregar_postgres.py, exceto as tabelas de uso
-- (geocode_cache e buscas), que são preservadas.

CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- minúsculas e sem acento; IMMUTABLE para poder ser usada em índice
CREATE OR REPLACE FUNCTION normaliza(t text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$ SELECT lower(public.unaccent('public.unaccent'::regdictionary, t)) $$;

-- ---------------------------------------------------------------- transporte (EPTC)
DROP TABLE IF EXISTS linha_paradas, horarios, itinerarios, frota, paradas, linhas, lugares CASCADE;

CREATE TABLE linhas (
    route_id   text PRIMARY KEY,
    numero     text NOT NULL,
    nome       text,
    cor        text,
    tracado    text NOT NULL DEFAULT 'antigo'   -- antigo | reconstruido
);
CREATE INDEX ON linhas (numero);

CREATE TABLE paradas (
    stop_id text PRIMARY KEY,
    nome    text NOT NULL,
    lat     double precision NOT NULL,
    lon     double precision NOT NULL
);
CREATE INDEX ON paradas (lat, lon);

-- sequência de paradas de cada linha e sentido, como está no GTFS gerado
CREATE TABLE linha_paradas (
    route_id text NOT NULL REFERENCES linhas,
    sentido  text NOT NULL,
    seq      int  NOT NULL,
    stop_id  text NOT NULL REFERENCES paradas,
    PRIMARY KEY (route_id, sentido, seq)
);
CREATE INDEX ON linha_paradas (stop_id);

-- tabela horária da EPTC, como veio no CSV (todas as tabelas, inclusive as sazonais)
CREATE TABLE horarios (
    linha        text NOT NULL,
    sentido      text NOT NULL,
    tipo_tabela  text NOT NULL,
    tipo_dia     text NOT NULL,
    horario      text NOT NULL,          -- HH:MM; pode passar de 24:00
    adaptado     boolean
);
CREATE INDEX ON horarios (linha, tipo_tabela, tipo_dia);

-- itinerário da EPTC: ruas e terminais em ordem
CREATE TABLE itinerarios (
    linha       text NOT NULL,
    sentido     text NOT NULL,
    seq         int  NOT NULL,
    tipo        text NOT NULL,           -- LOGRADOURO | TERMINAL/PARADA | PONTODEREFERENCIA
    nome        text,
    logradouro  text,
    codigo      text
);
CREATE INDEX ON itinerarios (linha, sentido, seq);

CREATE TABLE frota (
    prefixo         text,
    operador        text,
    placa           text,
    marca           text,
    modelo          text,
    ano_fabricacao  int,
    combustivel     text
);

-- ---------------------------------------------------------------- busca de lugares
-- ruas, lugares com nome, endereços com número (OpenStreetMap) e paradas (EPTC)
CREATE TABLE lugares (
    id       serial PRIMARY KEY,
    tipo     text NOT NULL,              -- rua | lugar | endereco | parada
    nome     text NOT NULL,
    detalhe  text,
    lat      double precision NOT NULL,
    lon      double precision NOT NULL,
    peso     real NOT NULL DEFAULT 1     -- desempate no ranking
);
CREATE INDEX lugares_nome_trgm ON lugares USING gin (normaliza(nome) gin_trgm_ops);

-- ---------------------------------------------------------------- uso (preservado)
CREATE TABLE IF NOT EXISTS geocode_cache (
    consulta   text PRIMARY KEY,         -- texto normalizado
    resultado  jsonb NOT NULL,
    criado_em  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS buscas (
    id         bigserial PRIMARY KEY,
    criado_em  timestamptz NOT NULL DEFAULT now(),
    de_nome    text,
    de_lat     double precision,
    de_lon     double precision,
    para_nome  text,
    para_lat   double precision,
    para_lon   double precision,
    quando     timestamptz,
    n_opcoes   int
);
