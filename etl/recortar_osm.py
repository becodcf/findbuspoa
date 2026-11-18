"""
Recorta o mapa OSM da região Sul (Geofabrik) para a área de Porto Alegre e arredores.

O recorte do BBBike usado antes terminava na latitude -30,152 e deixava de fora a Zona Sul
(Restinga, Lami, Belém Novo, Ponta Grossa): cerca de 900 paradas ficavam sem rua por perto
e o OpenTripPlanner não conseguia chegar a elas a pé.

Uso:  python etl/recortar_osm.py dados/osm/sul-latest.osm.pbf otp/poa.osm.pbf
"""
from __future__ import annotations

import sys
from pathlib import Path

import osmium

# Todas as paradas do GTFS ficam em lat -30,242..-29,970 e lon -51,281..-51,021.
# A margem de ~5 km cobre caminhadas e acessos pelos municípios vizinhos.
SUL, NORTE, OESTE, LESTE = -30.29, -29.92, -51.34, -50.97


def dentro(loc) -> bool:
    return loc.valid() and SUL <= loc.lat <= NORTE and OESTE <= loc.lon <= LESTE


def main(origem: Path, destino: Path) -> None:
    vias: set[int] = set()
    with osmium.BackReferenceWriter(str(destino), ref_src=str(origem), overwrite=True) as saida:
        for obj in osmium.FileProcessor(str(origem)).with_locations():
            if obj.is_node():
                # pontos soltos com nome ou endereço (lojas, hospitais, números de casa):
                # o OTP não usa, mas a busca de lugares no PostgreSQL sim
                if len(obj.tags) and dentro(obj.location) and (
                        "name" in obj.tags or "addr:housenumber" in obj.tags):
                    saida.add_node(obj)
            elif obj.is_way():
                if any(dentro(n.location) for n in obj.nodes):
                    vias.add(obj.id)
                    saida.add_way(obj)
            elif obj.is_relation():
                # restrições de conversão e relações de rota que só usam vias do recorte
                membros = list(obj.members)
                if membros and all(m.type != "w" or m.ref in vias for m in membros) and any(
                        m.type == "w" for m in membros) and obj.tags.get("type") in (
                        "restriction", "route", "multipolygon", "public_transport"):
                    saida.add_relation(obj)
    print(f"{len(vias)} vias gravadas em {destino}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
