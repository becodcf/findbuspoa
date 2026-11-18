"""
Backend do MVP UberBus (FastAPI).

Rotas:
  GET /                         -> página de teste (mapa)
  GET /api/sugestoes?q=...      -> lugares enquanto a pessoa digita (PostgreSQL, sem limite de uso)
  GET /api/geocode?q=...        -> endereço completo -> opções com coordenadas (Nominatim, com cache no PostgreSQL)
  GET /api/rota?...             -> planeja a viagem de ônibus (OpenTripPlanner local)
  GET /api/linhas?q=...         -> busca linhas
  GET /api/linhas/{numero}      -> paradas, itinerário e horários de uma linha
  GET /api/paradas/proximas     -> paradas perto de um ponto e as linhas que passam nelas
  GET /api/saude                -> verifica OTP e banco

Rodar:  .venv\\Scripts\\uvicorn backend.app:app --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend import db

OTP_URL = os.getenv("OTP_URL", "http://localhost:8080/otp/gtfs/v1")
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# A política do Nominatim exige um User-Agent que identifique a aplicação, no máximo
# 1 requisição por segundo e proíbe usar o serviço como autocompletar: por isso as
# sugestões enquanto se digita vêm do PostgreSQL, e o Nominatim só é chamado quando a
# pessoa pede o endereço exato (Enter ou "procurar endereço").
USER_AGENT = os.getenv("USER_AGENT", "UberBus-MVP/0.1")
TZ = ZoneInfo("America/Sao_Paulo")
# Caixa de Porto Alegre (oeste, norte, leste, sul), usada para limitar os resultados.
POA_VIEWBOX = "-51.31,-29.93,-51.01,-30.27"

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def ciclo(_app: FastAPI):
    yield
    db.fechar()


app = FastAPI(title="UberBus MVP", lifespan=ciclo)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

PLAN_QUERY = """
query Plano($origem: PlanLabeledLocationInput!, $destino: PlanLabeledLocationInput!,
            $quando: OffsetDateTime!, $n: Int!) {
  planConnection(
    origin: $origem
    destination: $destino
    dateTime: { earliestDeparture: $quando }
    first: $n
    modes: { transit: { transit: [{ mode: BUS }] } }
    preferences: {
      # Em POA cada baldeação normalmente é uma passagem nova: penaliza (600 ≈ 10 min)
      # e dá 2 min de folga para trocar de ônibus.
      transit: { transfer: { cost: 600, slack: "PT2M", maximumTransfers: 2 } }
    }
  ) {
    edges {
      node {
        start
        end
        duration
        walkDistance
        legs {
          mode
          start { scheduledTime }
          end { scheduledTime }
          duration
          distance
          headsign
          from { name lat lon }
          to { name lat lon }
          route { shortName longName color }
          legGeometry { points }
          intermediatePlaces { name }
        }
      }
    }
  }
}
"""


def _sem_banco(e: Exception) -> HTTPException:
    return HTTPException(503, f"Banco de dados indisponível: {e}")


@app.get("/")
def index() -> FileResponse:
    # sem cache: depois de atualizar o projeto, a página nova aparece sem Ctrl+F5
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/saude")
async def saude() -> dict:
    res: dict = {}
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(OTP_URL, json={"query": "{ feeds { feedId } }"})
            r.raise_for_status()
        res["otp"] = "ok"
    except Exception as e:  # noqa: BLE001
        res["otp"] = f"fora do ar: {e}"
    try:
        n = await run_in_threadpool(db.consulta, "SELECT count(*) AS n FROM lugares")
        res["banco"] = "ok"
        res["lugares"] = n[0]["n"]
    except Exception as e:  # noqa: BLE001
        res["banco"] = f"fora do ar: {e}"
    return res


# ---------------------------------------------------------------- busca de lugares
SQL_SUGESTOES = """
WITH q AS (SELECT normaliza(%(q)s) AS t)
SELECT tipo, nome, detalhe, lat, lon
FROM lugares, q
WHERE q.t <%% normaliza(nome) AND tipo <> 'endereco'
ORDER BY (normaliza(nome) LIKE q.t || '%%' OR normaliza(nome) LIKE '%% ' || q.t || '%%') DESC,
         round(word_similarity(q.t, normaliza(nome))::numeric, 1) DESC,
         (detalhe LIKE '%%Porto Alegre%%') DESC, peso DESC, length(nome)
LIMIT 40
"""
SQL_NUMERO = """
SELECT tipo, nome, detalhe, lat, lon FROM lugares
WHERE tipo = 'endereco' AND normaliza(%(rua)s) <%% normaliza(nome)
  AND normaliza(nome) LIKE '%%, ' || %(num)s
ORDER BY word_similarity(normaliza(%(rua)s), normaliza(nome)) DESC
LIMIT 5
"""
TIPO_ROTULO = {"rua": "Rua", "lugar": "Lugar", "endereco": "Endereço", "parada": "Parada"}


def _sugestoes(q: str, n: int) -> list[dict]:
    linhas = []
    m = re.match(r"^(.*?\D)[,\s]+(\d{1,5})\s*$", q)
    if m:  # "ramiro barcelos 2350": primeiro os endereços com esse número
        linhas += db.consulta(SQL_NUMERO, {"rua": m.group(1).strip(), "num": m.group(2)})
        q = m.group(1).strip()
    linhas += db.consulta(SQL_SUGESTOES, {"q": q})
    res, vistos = [], []
    for r in linhas:
        # a mesma rua aparece uma vez por cidade (avenidas longas têm vários trechos)
        cidade = (r["detalhe"] or "").split(", ")[-1]
        if any(v["nome"] == r["nome"] and (
                (r["tipo"] == "rua" and v["tipo"] == "rua" and (v["detalhe"] or "").split(", ")[-1] == cidade)
                or (abs(v["lat"] - r["lat"]) < 0.02 and abs(v["lon"] - r["lon"]) < 0.02)) for v in vistos):
            continue
        vistos.append(r)
        res.append({"nome": r["nome"], "detalhe": r["detalhe"] or "", "tipo": TIPO_ROTULO.get(r["tipo"], r["tipo"]),
                    "lat": r["lat"], "lon": r["lon"], "fonte": "local"})
        if len(res) >= n:
            break
    return res


@app.get("/api/sugestoes")
async def sugestoes(q: str = Query(..., min_length=2), n: int = Query(8, ge=1, le=20)) -> list[dict]:
    try:
        return await run_in_threadpool(_sugestoes, q.strip(), n)
    except Exception as e:  # noqa: BLE001
        raise _sem_banco(e) from e


_ultima_nominatim = 0.0
_trava_nominatim = asyncio.Lock()


def _rotulo_nominatim(x: dict) -> tuple[str, str]:
    """Rótulo curto ("Rua Ramiro Barcelos, 2350") + detalhe ("Santa Cecília, Porto Alegre")."""
    a = x.get("address", {})
    rua = a.get("road") or a.get("pedestrian") or a.get("footway") or ""
    nome = x.get("name") or ""
    principal = ", ".join(p for p in (rua, a.get("house_number")) if p)
    if nome and nome != rua:
        principal = f"{nome} ({principal})" if principal else nome
    bairro = a.get("suburb") or a.get("neighbourhood") or a.get("quarter") or ""
    cidade = a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or ""
    detalhe = ", ".join(p for p in (bairro, cidade, a.get("postcode")) if p)
    return principal or x["display_name"].split(",")[0], detalhe


async def _nominatim(q: str) -> list[dict]:
    global _ultima_nominatim
    params = {
        "q": q, "format": "jsonv2", "limit": 6, "countrycodes": "br", "addressdetails": 1,
        "viewbox": POA_VIEWBOX, "bounded": 1, "accept-language": "pt-BR",
    }
    async with _trava_nominatim:  # no máximo 1 requisição por segundo
        espera = 1.0 - (time.monotonic() - _ultima_nominatim)
        if espera > 0:
            await asyncio.sleep(espera)
        try:
            async with httpx.AsyncClient(timeout=10, headers={"User-Agent": USER_AGENT}) as c:
                r = await c.get(NOMINATIM_URL, params=params)
                r.raise_for_status()
        finally:
            _ultima_nominatim = time.monotonic()
    res = []
    for x in r.json():
        nome, detalhe = _rotulo_nominatim(x)
        res.append({"nome": nome, "detalhe": detalhe, "tipo": "Endereço", "completo": x["display_name"],
                    "lat": float(x["lat"]), "lon": float(x["lon"]), "fonte": "nominatim"})
    return res


@app.get("/api/geocode")
async def geocode(q: str = Query(..., min_length=3)) -> list[dict]:
    """Opções de endereço para a pessoa escolher (não escolhe sozinho)."""
    chave = " ".join(q.lower().split())
    try:
        cache = await run_in_threadpool(
            db.consulta, "SELECT resultado FROM geocode_cache WHERE consulta = %s", (chave,))
    except Exception:  # noqa: BLE001  banco fora do ar: segue sem cache
        cache = None
    if cache:
        return cache[0]["resultado"]
    try:
        res = await _nominatim(q.strip())
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Erro no Nominatim: {e}") from e
    try:
        await run_in_threadpool(
            db.executa,
            "INSERT INTO geocode_cache (consulta, resultado) VALUES (%s, %s) "
            "ON CONFLICT (consulta) DO UPDATE SET resultado = EXCLUDED.resultado, criado_em = now()",
            (chave, json.dumps(res, ensure_ascii=False)))
    except Exception:  # noqa: BLE001
        pass
    return res


# ---------------------------------------------------------------- rotas
def _ms_para_hora(iso: str | None) -> str | None:
    return datetime.fromisoformat(iso).astimezone(TZ).strftime("%H:%M") if iso else None


@app.get("/api/rota")
async def rota(
    de_lat: float, de_lon: float, para_lat: float, para_lon: float,
    quando: datetime | None = Query(None, description="ISO 8601; padrão = agora"),
    n: int = Query(3, ge=1, le=6),
    de_nome: str | None = None, para_nome: str | None = None,
) -> dict:
    quando = (quando or datetime.now(TZ))
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=TZ)
    variaveis = {
        "origem": {"location": {"coordinate": {"latitude": de_lat, "longitude": de_lon}}},
        "destino": {"location": {"coordinate": {"latitude": para_lat, "longitude": para_lon}}},
        "quando": quando.isoformat(),
        "n": n,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(OTP_URL, json={"query": PLAN_QUERY, "variables": variaveis})
            r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(503, f"OpenTripPlanner indisponível: {e}") from e
    corpo = r.json()
    if corpo.get("errors"):
        raise HTTPException(502, corpo["errors"])

    opcoes = []
    for edge in corpo["data"]["planConnection"]["edges"]:
        it = edge["node"]
        so_a_pe = all(leg["mode"] == "WALK" for leg in it["legs"])
        if so_a_pe and it["walkDistance"] > 1500:
            continue  # o OTP às vezes sugere caminhar vários km; não faz sentido num app de ônibus
        opcoes.append({
            "saida": _ms_para_hora(it["start"]),
            "chegada": _ms_para_hora(it["end"]),
            "duracao_min": round(it["duration"] / 60),
            "caminhada_m": round(it["walkDistance"]),
            "trechos": [{
                "modo": "ONIBUS" if leg["mode"] == "BUS" else "A_PE" if leg["mode"] == "WALK" else leg["mode"],
                "saida": _ms_para_hora(leg["start"]["scheduledTime"]),
                "chegada": _ms_para_hora(leg["end"]["scheduledTime"]),
                "duracao_min": round(leg["duration"] / 60),
                "distancia_m": round(leg["distance"]),
                "de": leg["from"], "para": leg["to"],
                "linha": leg["route"]["shortName"] if leg["route"] else None,
                "nome_linha": leg["route"]["longName"] if leg["route"] else None,
                "cor": leg["route"]["color"] if leg["route"] else None,
                "sentido": leg["headsign"],
                "paradas": len(leg["intermediatePlaces"] or []),
                "geometria": leg["legGeometry"]["points"],  # polyline codificada (Google)
            } for leg in it["legs"]],
        })
    try:  # registro da busca (estatística de uso); falha no banco não impede a resposta
        await run_in_threadpool(
            db.executa,
            "INSERT INTO buscas (de_nome, de_lat, de_lon, para_nome, para_lat, para_lon, quando, n_opcoes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (de_nome, de_lat, de_lon, para_nome, para_lat, para_lon, quando, len(opcoes)))
    except Exception:  # noqa: BLE001
        pass
    return {"quando": quando.isoformat(), "opcoes": opcoes}


# ---------------------------------------------------------------- linhas e paradas
@app.get("/api/linhas")
async def linhas(q: str = Query("", description="número ou parte do nome")) -> list[dict]:
    try:
        return await run_in_threadpool(
            db.consulta,
            "SELECT numero, nome, tracado FROM linhas "
            "WHERE %(q)s = '' OR numero ILIKE %(q)s || '%%' OR normaliza(nome) LIKE '%%' || normaliza(%(q)s) || '%%' "
            "ORDER BY numero LIMIT 50", {"q": q.strip()})
    except Exception as e:  # noqa: BLE001
        raise _sem_banco(e) from e


def _linha(numero: str) -> dict:
    info = db.consulta("SELECT route_id, numero, nome, cor, tracado FROM linhas WHERE upper(numero) = upper(%s)",
                       (numero,))
    if not info:
        raise HTTPException(404, f"Linha {numero} não está no feed")
    l = info[0]
    paradas = db.consulta(
        "SELECT lp.sentido, lp.seq, p.stop_id, p.nome, p.lat, p.lon FROM linha_paradas lp "
        "JOIN paradas p USING (stop_id) WHERE lp.route_id = %s ORDER BY lp.sentido, lp.seq", (l["route_id"],))
    ruas = db.consulta(
        "SELECT sentido, logradouro FROM itinerarios WHERE linha = %s AND tipo = 'LOGRADOURO' "
        "ORDER BY sentido, seq", (l["numero"],))
    horarios = db.consulta(
        "SELECT sentido, tipo_tabela, tipo_dia, array_agg(horario ORDER BY horario) AS saidas "
        "FROM horarios WHERE linha = %s GROUP BY sentido, tipo_tabela, tipo_dia "
        "ORDER BY sentido, tipo_tabela, tipo_dia", (l["numero"],))
    sentidos: dict[str, dict] = {}
    for p in paradas:
        sentidos.setdefault(p["sentido"], {"paradas": [], "ruas": []})["paradas"].append(
            {k: p[k] for k in ("stop_id", "nome", "lat", "lon")})
    for r in ruas:
        s = sentidos.setdefault(r["sentido"], {"paradas": [], "ruas": []})
        if not s["ruas"] or s["ruas"][-1] != r["logradouro"]:
            s["ruas"].append(r["logradouro"])
    return {"numero": l["numero"], "nome": l["nome"], "cor": l["cor"], "tracado": l["tracado"],
            "sentidos": sentidos, "horarios": horarios}


@app.get("/api/linhas/{numero}")
async def linha(numero: str) -> dict:
    try:
        return await run_in_threadpool(_linha, numero)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise _sem_banco(e) from e


@app.get("/api/paradas/proximas")
async def paradas_proximas(lat: float, lon: float, raio: int = Query(400, ge=50, le=2000)) -> list[dict]:
    # caixa em graus para usar o índice e depois a distância de verdade (haversine)
    dlat, dlon = raio / 111_000, raio / 96_000
    sql = """
    SELECT p.stop_id, p.nome, p.lat, p.lon,
           round(2 * 6371000 * asin(sqrt(power(sin(radians(p.lat - %(lat)s) / 2), 2)
                 + cos(radians(%(lat)s)) * cos(radians(p.lat)) * power(sin(radians(p.lon - %(lon)s) / 2), 2)))) AS distancia_m,
           array_agg(DISTINCT l.numero ORDER BY l.numero) AS linhas
    FROM paradas p JOIN linha_paradas lp USING (stop_id) JOIN linhas l USING (route_id)
    WHERE p.lat BETWEEN %(lat)s - %(dlat)s AND %(lat)s + %(dlat)s
      AND p.lon BETWEEN %(lon)s - %(dlon)s AND %(lon)s + %(dlon)s
    GROUP BY p.stop_id
    HAVING min(2 * 6371000 * asin(sqrt(power(sin(radians(p.lat - %(lat)s) / 2), 2)
                 + cos(radians(%(lat)s)) * cos(radians(p.lat)) * power(sin(radians(p.lon - %(lon)s) / 2), 2)))) <= %(raio)s
    ORDER BY distancia_m LIMIT 20
    """
    try:
        return await run_in_threadpool(db.consulta, sql,
                                       {"lat": lat, "lon": lon, "dlat": dlat, "dlon": dlon, "raio": raio})
    except Exception as e:  # noqa: BLE001
        raise _sem_banco(e) from e
