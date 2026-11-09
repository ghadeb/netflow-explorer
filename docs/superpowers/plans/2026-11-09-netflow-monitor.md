# NetFlow Monitor Implementation Plan

**Objectivo** — Entregar um stack Docker Compose instalável manualmente num servidor remoto para NetFlow v9 IPv4 em ambos os sentidos. Guardar registos em SQLite durante 30 dias e apresentar um painel privado em HTTPS.

**Arquitectura** — O GoFlow2 descodifica UDP/2055 (entrada na LAN) e UDP/2056 (saída). O processo Python valida o IP do exportador, guarda o JSON normalizado e mantém agregados por minuto por IP interno, origem e porto de destino na mesma transacção. React consulta uma API de leitura. Caddy termina TLS e exige autenticação.

- [x] `collector/storage.py` — testar classificação por IP de origem/destino, separação por exportador, contagem de fluxos, limites de retenção e transacções entre dados e agregados. Validar com `python3 -m unittest discover -s tests`.
- [x] `collector/server.py` — lançar duas instâncias GoFlow2 com portas distintas, persistir em lotes, indicar falhas e servir `/api/dashboard` e o painel. Usar o instante de recepção para janelas e retenção. Verificar a API com registos sintéticos.
- [ ] `web/` — seguir o conceito Fluxo, com cabeçalho, filtros, totais, série temporal e ranking. Verificar a compilação e a vista responsiva com dados de teste.
- [x] `compose.yaml`, `Dockerfile`, `Caddyfile`, `README.md` — proteger o painel e restringir portas a interfaces configuradas. Documentar o setup manual e a mudança/rollback dos exporters. Verificar a renderização do Compose e preparar uma prova UDP para o operador.

O teste isolado em Docker enviou templates e dados NetFlow v9 aos dois sockets UDP e confirmou gravação em SQLite e leitura na API. A compilação do frontend passou. A inspecção no navegador, TLS e a validação com os equipamentos reais ficam pendentes enquanto a prioridade é iniciar a recolha.
