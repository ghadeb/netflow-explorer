# NetFlow Reporter

Fluxo Explorer é o painel desta versão. Apresenta fluxos individuais, gráficos de padrões, pesquisa, paginação, agregações, ASN quando exportado, PTR a pedido e exportação CSV. O mesmo serviço `collector` recebe NetFlow e serve HTTPS na porta 8001. O selector no cabeçalho alterna entre Português e English e guarda a preferência no navegador.

O GoFlow2 recebe NetFlow v9 IPv4 em UDP/2055 e UDP/2056. O serviço `collector` grava cada registo aceite em SQLite com o exportador, o sentido, os contadores e o JSON normalizado pelo GoFlow2. A base fica no volume persistente `fluxo_sqlite-data`. A retenção predefinida é de 30 dias a partir da recepção.

## 1. Iniciar a recolha no servidor Linux

Instala ou confirma Docker Engine e o plugin Docker Compose. Usa um servidor Linux AMD64 e entra na pasta `netflow-reporter`.

```sh
cp .env.example .env
```

Edita `.env`. Define `NETFLOW_UDP_BIND` com o IPv4 real do servidor que os routers alcançam e `FLOW_EXPORTERS` com os IPv4 de origem dos datagramas NetFlow. Define `NETFLOW_WEB_BIND` com o IPv4 privado usado para consultar o painel. Prepara o certificado em `tls-fluxo/` conforme a [configuração HTTPS](#4-configurar-o-https). Se algum router usa uma VRF para chegar à probe, confirma essa rota e a sintaxe de `destination <IP> vrf <VRF>` no CLI antes de aplicar.

Autoriza UDP/2055 e UDP/2056 no caminho a partir dos dois IP dos exporters. Num servidor com Docker, verifica a filtragem efectiva do tráfego publicado pelo Docker. Usa um caminho privado sem NAT entre routers e colector quando precisares de os distinguir pelo IP de origem.

```sh
docker compose config -q
docker compose up -d --build collector
docker compose ps
docker compose logs --tail=30 collector
```

O painel e a API são servidos pelo colector em HTTPS. O endereço é `https://<NETFLOW_WEB_BIND>:8001`.

## 2. Configurar os routers

Substitui `<IP-PROBE>` pelo mesmo IPv4 de `NETFLOW_UDP_BIND`, `<IF-SOURCE>` pela interface de origem já existente e `<IF-LAN>` pela interface L3 virada aos clientes. Faz primeiro num router e valida antes de repetir no segundo.

```ios
show version
show ip route <IP-PROBE>
show running-config interface <IF-LAN>
show flow monitor
show flow exporter
show platform hardware qfp active datapath utilization
```

Para os monitores `TOP-IPV4` e `TOP-IPV4-DOWN` já aplicados na LAN, acrescenta apenas os exporters e as associações abaixo. Confirma os nomes e que os sentidos correspondem. Os exporters de entrada e saída usam portas diferentes para a probe identificar o sentido. Se a imagem não aceitar acrescentar um exporter a um monitor activo, revê o âmbito de todas as interfaces associadas antes de desassociar qualquer monitor existente.

```ios
configure terminal
 flow exporter FLUXO-UP
  destination <IP-PROBE>
  source <IF-SOURCE>
  transport udp 2055
  export-protocol netflow-v9
  template data timeout 60
 exit
 flow exporter FLUXO-DOWN
  destination <IP-PROBE>
  source <IF-SOURCE>
  transport udp 2056
  export-protocol netflow-v9
  template data timeout 60
 exit
 flow monitor TOP-IPV4
  exporter FLUXO-UP
 exit
 flow monitor TOP-IPV4-DOWN
  exporter FLUXO-DOWN
 end
```

Se os monitores ainda não existem, configura os dois exporters primeiro. Depois usa o bloco abaixo em vez das duas associações simples. O timeout activo de 60 segundos ajuda a obter dados de fluxos que continuam activos durante o teste.

```ios
configure terminal
 flow monitor TOP-IPV4
  record netflow ipv4 original-input
  exporter FLUXO-UP
  cache timeout active 60
 exit
 flow monitor TOP-IPV4-DOWN
  record netflow ipv4 original-output
  exporter FLUXO-DOWN
  cache timeout active 60
 exit
 interface <IF-LAN>
  ip flow monitor TOP-IPV4 input
  ip flow monitor TOP-IPV4-DOWN output
 end
```

Se só existir um dos monitores, acrescenta o exporter ao monitor existente e usa o bloco de criação apenas para o sentido em falta. Para um monitor existente com timeout maior, o primeiro registo pode chegar quando o fluxo ficar inactivo. Gera tráfego IPv4 de teste de curta duração num cliente atrás da LAN e aguarda a expiração.

```ios
show flow interface <IF-LAN>
show flow exporter FLUXO-UP statistics
show flow exporter FLUXO-DOWN statistics
show flow monitor TOP-IPV4
show flow monitor TOP-IPV4-DOWN
show platform hardware qfp active datapath utilization
```

Confirma que os contadores de envio sobem. No servidor, se a base continuar vazia, verifica primeiro se chegam datagramas e se o IP de origem coincide com `FLOW_EXPORTERS`. A recepção de pacotes por si só não prova a descodificação dos templates nem a gravação em SQLite.

```sh
sudo tcpdump -ni <IF-SERVIDOR> -c 20 'udp dst port 2055 or udp dst port 2056'
docker compose logs --tail=100 collector
```

Documentação Cisco para [exportação NetFlow v9](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/fnetflow/configuration/xe-16/fnf-xe-16-book/fnf-v9-export.html), [IOS XE 16.9](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/fnetflow/configuration/xe-16-9/fnf-xe-16-9-book/fnf-fnetflow.html) e [ASR1000 IOS XE 3S](https://www.cisco.com/c/ja_jp/td/docs/rt/wanaggregationinternetedgert/asr1000aggregationservsrt/cg/010/fnf-xe-3s-asr1000-book-1.pdf).

### Reversão da exportação

Para monitores que já existiam, remove apenas as associações adicionadas e depois os exporters. Se também criaste os monitores para este teste, podes desassociá-los da interface e removê-los. Confirma com `show flow interface <IF-LAN>`.

```ios
configure terminal
 flow monitor TOP-IPV4
  no exporter FLUXO-UP
 exit
 flow monitor TOP-IPV4-DOWN
  no exporter FLUXO-DOWN
 exit
 no flow exporter FLUXO-UP
 no flow exporter FLUXO-DOWN
end
```

## 3. Provar a gravação e extrair a evidência

A consulta seguinte mostra os últimos 15 minutos. Espera linhas `up` e `down` para cada router onde circulou tráfego nos dois sentidos.

```sh
docker compose exec -T collector python - <<'PY'
import sqlite3
db = sqlite3.connect("file:/data/flows.sqlite3?mode=ro", uri=True)
for row in db.execute("""
    SELECT router, direction, count(*), sum(bytes),
           datetime(min(received), 'unixepoch'), datetime(max(received), 'unixepoch')
    FROM records WHERE received >= unixepoch('now', '-15 minutes')
    GROUP BY router, direction ORDER BY router, direction
"""):
    print(*row, sep="\t")
PY
```

Colunas por ordem — exportador, sentido, registos, bytes, primeira e última recepção em UTC. Guarda a saída com a hora, a identificação do router, a interface observada e os contadores Cisco para documentar o ponto de captura.

Para guardar uma cópia SQLite consistente enquanto a ingestão continua, usa a API de backup do SQLite. Reserva espaço para a cópia e protege o ficheiro porque contém metadados de tráfego. Confirma que o comando termina com sucesso antes de calcular o hash.

```sh
umask 077
snapshot="fluxo-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
docker compose exec -T collector python - > "$snapshot" <<'PY'
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "snapshot.sqlite3"
    source = sqlite3.connect("file:/data/flows.sqlite3?mode=ro", uri=True)
    target = sqlite3.connect(path)
    source.backup(target)
    target.close()
    source.close()
    with path.open("rb") as stream:
        shutil.copyfileobj(stream, sys.stdout.buffer)
PY
sha256sum "$snapshot" > "$snapshot.sha256"
```

## 4. Configurar e verificar o HTTPS

Antes de arrancar, corre na raiz do repositório no servidor. Substitui o endereço
de exemplo pelo IPv4 ou nome DNS que vais usar para abrir o painel.

```sh
./scripts/tls.sh 192.0.2.20
```

O comando gera um certificado autoassinado quando ainda não existe um par,
ou verifica o par existente. Confirma a validade, o endereço ou nome do painel e
a correspondência com a chave privada. Os ficheiros ficam em
`tls-fluxo/cert.pem` e `tls-fluxo/key.pem`. O contentor lê a chave com GID 10001.

Para usar a tua CA, coloca o certificado do servidor e os intermédios em
`tls-fluxo/cert.pem`, a chave PEM sem passphrase em `tls-fluxo/key.pem` e a CA
raiz em `tls-fluxo/ca.pem`. Corre o mesmo comando para validar a cadeia.
Se o certificado for para um nome DNS, coloca esse nome em `TLS_VERIFY_HOST`
no `.env`. Por omissão, o healthcheck usa `NETFLOW_WEB_BIND` para verificar o
certificado apresentado pelo contentor.

Depois de arrancar, verifica a ligação usando o ficheiro de confiança indicado
pelo script. O exemplo usa o certificado autoassinado:

```sh
curl --cacert tls-fluxo/cert.pem https://192.0.2.20:8001/healthz
```

O acesso directo destina-se à rede privada e a configuração base usa TLS sem login.

## 5. Painel e arquivos diários

- **Em directo** mostra os dados recentes, a distribuição do volume por porto e
  serviço e a repartição de bytes enviados e recebidos. **Pico de actividade**
  indica o intervalo UTC com mais bytes nos registos recebidos pelo colector.
  O painel actualiza a cada dez minutos e também com **Actualizar agora**.
- **Arquivos** lista os dias disponíveis no volume, do mais recente para o mais
  antigo, com o tamanho comprimido. Selecciona um dia para pesquisar, visualizar
  e exportar os seus fluxos. Também podes carregar um `fluxo-AAAA-MM-DD.sqlite3.xz`
  do teu computador. O contentor verifica o arquivo e abre uma cópia só de leitura
  em `/archives/.imports`, separada do SQLite activo. Ao abrir outro dia, a cópia
  de consulta anterior é substituída.
- **Weathermap** mostra os pares de um host IPv4 nos dois sentidos ou os destinos
  de uma rede de origem CIDR, paginados por volume. Em fluxos individuais,
  **Ver mapa** mostra os endpoints, o exporter e o próximo salto ou interfaces
  quando exportados. As linhas tracejadas assinalam troços sem visibilidade.
  O mapa usa a origem de dados e os filtros do painel seleccionado.

Para resolver um PTR a pedido, configura os resolvedores internos em `PTR_DNS`
no `.env`, separados por vírgulas. O contentor tem de os alcançar pela porta 53.
O diagnóstico seguinte usa os mesmos resolvedores e regras que o botão PTR.
Substitui o IP do exemplo por um endereço com PTR conhecido.

```sh
docker compose exec -T collector python /painel/ptr.py 192.0.2.11
```

A recolha mantém por omissão 30 dias de consulta no SQLite. O processo de
arquivo cria um `sqlite3.xz` por dia UTC com registos e um ficheiro SHA-256
no volume `fluxo-archives` (montado em `/archives`). A compressão usa XZ no
nível 9 com o modo extreme. O processo verifica os dias completos após as
00:15 UTC e recupera os dias em falta que ainda existam no SQLite. Um dia só
pode ser removido do SQLite quando estiver inteiramente fora da retenção e as
linhas constarem do arquivo verificado.

`FLOW_ARCHIVE_VOLUME` permite indicar outro volume. Para um volume externo
já existente, conserva o override `-f compose.yaml -f compose.archives.yaml`
e confirma acesso de escrita para UID/GID 10001. Os ficheiros arquivados
não expiram automaticamente. Para copiar um dia do volume para guardar fora do
contentor, ajusta a data deste exemplo.

```sh
docker compose cp collector:/archives/fluxo-2026-11-08.sqlite3.xz .
```

## Capacidade e retenção

SQLite usa WAL, um escritor e agregados por minuto. A expiração reutiliza
espaço na base. O tamanho depende dos registos por segundo e do número de
IPs e portos distintos. A exportação precisa de espaço temporário para o
SQLite de um dia e o respectivo XZ. Uma consulta de 30 dias pode exceder o
tempo limite em instalações de grande volume. Reduz o período ou aplica filtros.

```sh
docker compose exec collector du -h /data /archives
docker compose logs --tail=100 collector
```

A ingestão guarda os registos IPv4 v9 descodificados dos exporters autorizados. UDP pode perder datagramas e a sonda precisa de receber os templates para descodificar os registos. Na fase inicial, compara os contadores Cisco, os logs da probe e a consulta SQLite. Para parar os serviços e conservar o volume, usa `docker compose stop`. [GoFlow2](https://github.com/netsampler/goflow2/tree/v2.2.7)
