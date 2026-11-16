# Sobe o OpenTripPlanner e o backend FastAPI.
#   .\iniciar.ps1            -> usa o grafo já construído
#   .\iniciar.ps1 -Rebuild   -> regenera o GTFS a partir de dados\ e reconstrói o grafo
#   .\iniciar.ps1 -Validar   -> roda o gtfs-validator (MobilityData) no GTFS gerado e sai
param([switch]$Rebuild, [switch]$Validar)
$ErrorActionPreference = "Stop"
$raiz = $PSScriptRoot
$py = Join-Path $raiz ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    python -m venv (Join-Path $raiz ".venv")
    & $py -m pip install -r (Join-Path $raiz "requirements.txt")
}

$downloads = [ordered]@{
    "otp\otp.jar"                       = "https://repo1.maven.org/maven2/org/opentripplanner/otp-shaded/2.9.0/otp-shaded-2.9.0.jar"
    "ferramentas\gtfs-validator-cli.jar" = "https://github.com/MobilityData/gtfs-validator/releases/download/v8.0.1/gtfs-validator-8.0.1-cli.jar"
}
foreach ($arq in $downloads.Keys) {
    $destino = Join-Path $raiz $arq
    if (-not (Test-Path $destino)) {
        Write-Host "Baixando $arq ..."
        New-Item -ItemType Directory -Force (Split-Path $destino) | Out-Null
        Invoke-WebRequest -Uri $downloads[$arq] -OutFile $destino
    }
}

# Mapa OSM: extrato da região Sul (Geofabrik, ~430 MB) recortado para POA e arredores.
# O recorte do BBBike usado antes não cobria a Zona Sul (Restinga, Lami, Belém Novo).
$osm = Join-Path $raiz "otp\poa.osm.pbf"
if (-not (Test-Path $osm)) {
    $sul = Join-Path $raiz "dados\osm\sul-latest.osm.pbf"
    if (-not (Test-Path $sul)) {
        Write-Host "Baixando mapa OSM da região Sul (Geofabrik, ~430 MB) ..."
        New-Item -ItemType Directory -Force (Split-Path $sul) | Out-Null
        Invoke-WebRequest -Uri "https://download.geofabrik.de/south-america/brazil/sul-latest.osm.pbf" -OutFile $sul
    }
    Write-Host "Recortando o mapa para Porto Alegre (alguns minutos) ..."
    & $py (Join-Path $raiz "etl\recortar_osm.py") $sul $osm
    $Rebuild = $true
}

if ($Rebuild -or -not (Test-Path (Join-Path $raiz "otp\graph.obj"))) {
    & $py (Join-Path $raiz "etl\gerar_gtfs.py")
    if ($LASTEXITCODE -ne 0) { throw "Falha ao gerar o GTFS" }
    Push-Location (Join-Path $raiz "otp")
    java -Xmx8G -jar otp.jar --build --save .
    Pop-Location
}

if ($Validar) {
    $saida = Join-Path $raiz "otp\validacao"
    java -Xmx4G -jar (Join-Path $raiz "ferramentas\gtfs-validator-cli.jar") -i (Join-Path $raiz "otp\gtfs-poa.zip") -o $saida
    Write-Host "Relatório: $saida\report.html"
    exit
}

Write-Host "Iniciando OpenTripPlanner (porta 8080)..."
Start-Process java -ArgumentList "-Xmx6G", "-jar", "otp.jar", "--load", "." -WorkingDirectory (Join-Path $raiz "otp")

Write-Host "Iniciando backend em http://localhost:8000 ..."
Set-Location $raiz
& (Join-Path $raiz ".venv\Scripts\uvicorn.exe") backend.app:app --port 8000
