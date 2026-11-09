const FluxoI18n = (() => {
  const english = {
    'Idioma': 'Language',
    'Padrões': 'Patterns',
    'Fluxos': 'Flows',
    'Explorador de tráfego': 'Traffic explorer',
    'NetFlow v9 · IPv4 · Hora UTC': 'NetFlow v9 · IPv4 · UTC time',
    'Pesquisar': 'Search',
    'IP, porto, serviço, protocolo ou AS…': 'IP, port, service, protocol or AS…',
    'Período': 'Time range',
    '1 hora': '1 hour',
    '6 horas': '6 hours',
    '24 horas': '24 hours',
    '7 dias': '7 days',
    '30 dias': '30 days',
    'Todos': 'All',
    'Sentido': 'Direction',
    'Ambos': 'Both',
    'Enviado': 'Sent',
    'Recebido': 'Received',
    '● Enviado': '● Sent',
    '● Recebido': '● Received',
    'Agrupar por': 'Group by',
    'Fluxos individuais': 'Individual flows',
    'IP de origem': 'Source IP',
    'IP de destino': 'Destination IP',
    'Origem → destino': 'Source → destination',
    'Rede de origem': 'Source network',
    'Rede de destino': 'Destination network',
    'Porto de destino': 'Destination port',
    'ASN de destino': 'Destination ASN',
    'Prefixo': 'Prefix length',
    'Aplicar': 'Apply',
    'Limpar': 'Clear',
    'Volume ao longo do tempo': 'Traffic volume over time',
    'Protocolos': 'Protocols',
    'Top origens': 'Top sources',
    'Top destinos': 'Top destinations',
    'Portos e serviços de destino': 'Destination ports and services',
    'Conversas · origem → destino': 'Conversations · source → destination',
    'ASNs de destino exportados': 'Exported destination ASNs',
    'Intensidade por dia e hora': 'Traffic by day and hour',
    'UTC · escolhe 7 dias para comparar a semana': 'UTC · choose 7 days to compare the week',
    'Ordenar': 'Sort',
    'Maior consumo': 'Highest volume',
    'Mais enviado': 'Most sent',
    'Mais recebido': 'Most received',
    'Mais pacotes': 'Most packets',
    'Mais recentes': 'Most recent',
    '↓ Exportar CSV': '↓ Export CSV',
    '← Anterior': '← Previous',
    'Seguinte →': 'Next →',
    'Linhas por página': 'Rows per page',
    'Os sentidos referem-se à LAN monitorizada. Se os dois routers observam o mesmo tráfego, selecciona um router para evitar somar as duas observações.':
      'Directions are relative to the monitored LAN. If both routers see the same traffic, select one router to avoid counting it twice.',
    'Sem dados para estes filtros.': 'No data for these filters.',
    'A consultar o SQLite…': 'Querying SQLite…',
    'Consulta até': 'Data through',
    'Volume total': 'Total volume',
    'Registos': 'Records',
    'por intervalo': 'per interval',
    'Volume enviado e recebido por intervalo': 'Sent and received volume per interval',
    'Passa o cursor para ver o detalhe por intervalo.': 'Hover to see the volume for each interval.',
    'Sem ASN de destino exportado neste intervalo.': 'No destination ASN was exported in this interval.',
    'Seg': 'Mon',
    'Ter': 'Tue',
    'Qua': 'Wed',
    'Qui': 'Thu',
    'Sex': 'Fri',
    'Sáb': 'Sat',
    'Dom': 'Sun',
    'Resolver': 'Resolve',
    'Sem PTR': 'No PTR record',
    'Tráfego agregado': 'Aggregated traffic',
    'registo': 'record',
    'registos': 'records',
    'grupo': 'group',
    'grupos': 'groups',
    'Recepção UTC': 'Received at (UTC)',
    'Origem': 'Source',
    'Destino': 'Destination',
    'Protocolo / serviço': 'Protocol / service',
    'ASN origem / destino': 'Source / destination ASN',
    'Pacotes': 'Packets',
    'Grupo': 'Group',
    'Sem resultados para os filtros seleccionados.': 'No results match the selected filters.',
    'Página': 'Page',
    'de': 'of',
    'Desconhecido': 'Unknown',
    'Sem dados': 'No data',
    'Página inexistente.': 'Page not found.',
    'O explorador está ocupado. Tenta novamente.': 'The explorer is busy. Try again.',
    'O explorador está ocupado.': 'The explorer is busy.',
    'Filtros inválidos ou consulta antiga. Actualiza o painel.': 'Invalid filters or an expired query. Refresh the dashboard.',
    'A exportação excede 50 000 linhas. Reduz o período ou aplica um filtro.':
      'The export exceeds 50,000 rows. Shorten the time range or apply a filter.',
    'Consulta indisponível. Experimenta um período mais curto.': 'Query unavailable. Try a shorter time range.',
    'Consulta indisponível.': 'Query unavailable.',
    'IPv4 inválido.': 'Invalid IPv4 address.',
    'DNS ocupado.': 'DNS resolver is busy.',
    'DNS indisponível.': 'DNS resolver is unavailable.',
    'Não foi possível ligar ao servidor.': 'Could not reach the server.',
    'Em directo · últimos 7 dias': 'Live · last 7 days',
    '↑ Importar arquivo .xz': '↑ Import .xz archive',
    'Voltar ao directo': 'Back to live',
    'Arquivo UTC': 'UTC archive',
    'A importar e verificar o arquivo…': 'Importing and verifying the archive…',
    'Já está a decorrer uma importação.': 'An import is already in progress.',
    'Escolhe um arquivo XZ até 2 GiB com espaço suficiente no disco.':
      'Choose an XZ archive up to 2 GiB and make sure there is enough free disk space.',
    'Arquivo XZ incompleto.': 'Incomplete XZ archive.',
    'O arquivo descomprimido excede o espaço disponível.': 'The decompressed archive exceeds the available disk space.',
    'O ficheiro deve conter um único arquivo XZ.': 'The file must contain a single XZ archive.',
    'O ficheiro não é um arquivo diário Fluxo.': 'The file is not a Fluxo daily archive.',
    'Estrutura do arquivo inválida.': 'Invalid archive structure.',
    'O SQLite do arquivo falhou a verificação.': 'The archive SQLite integrity check failed.',
    'O arquivo deve representar um dia.': 'The archive must contain exactly one day.',
    'Datas ou contagem de fluxos inválidas.': 'Invalid dates or flow count.',
    'Importação recusada.': 'Import rejected.',
    'A consulta em directo abrange até 7 dias.': 'Live queries cover up to 7 days.',
    'Arquivo inválido.': 'Invalid archive.',
    'Importa novamente o arquivo.': 'Import the archive again.',
  };

  function chooseLanguage(saved, preferences = []) {
    if (saved === 'pt' || saved === 'en') return saved;
    for (const preference of preferences) {
      const code = preference.toLowerCase().split('-')[0];
      if (code === 'pt' || code === 'en') return code;
    }
    return 'pt';
  }

  function translate(message, language) {
    if (language !== 'en') return message;
    if (Object.hasOwn(english, message)) return english[message];
    for (const prefix of ['Importação recusada.', 'Consulta indisponível.']) {
      if (message.startsWith(prefix + ' ')) {
        return english[prefix] + ' ' + translate(message.slice(prefix.length + 1), language);
      }
    }
    return message;
  }

  return {chooseLanguage, translate};
})();

if (typeof module !== 'undefined') module.exports = FluxoI18n;
