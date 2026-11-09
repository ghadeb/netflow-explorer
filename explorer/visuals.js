function makePagePicker(id, navigate) {
  const root = $('page-picker-template').content.firstElementChild.cloneNode(true);
  $(id).append(root);
  const summary = root.querySelector('summary');
  const menu = root.querySelector('form');
  const input = menu.elements.page;
  const shortcuts = root.querySelector('.page-shortcuts');
  let available = false;
  let page, pages;
  function choose(value) {
    if (!available || !Number.isSafeInteger(value) || value < 1 || value > pages) return;
    root.open = false;
    summary.focus();
    if (value !== page) navigate(value);
  }

  summary.onclick = event => { if (!available) event.preventDefault(); };
  root.ontoggle = () => {
    if (root.open) {
      input.value = '';
      input.focus();
    }
  };
  root.onkeydown = event => {
    if (event.key === 'Escape' && root.open) {
      event.preventDefault();
      root.open = false;
      summary.focus();
    }
  };
  menu.onsubmit = event => {
    event.preventDefault();
    choose(input.valueAsNumber);
  };
  shortcuts.onclick = event => {
    const button = event.target.closest('[data-page]');
    if (button) choose(Number(button.dataset.page));
  };

  return (data, busy = false) => {
    page = data?.page ?? 1;
    pages = data ? Math.max(1, Math.ceil(data.matches / data.size)) : 1;
    available = pages > 1 && !busy;
    summary.textContent = data
      ? t('Página') + ' ' + fmt.format(page) + ' ' + t('de') + ' ' + fmt.format(pages)
      : t('Página') + ' —';
    summary.setAttribute('aria-disabled', String(!available));
    input.max = pages;
    input.placeholder = String(page);
    if (!available) root.open = false;
    const choices = [...new Set([1, page-2, page-1, page, page+1, page+2, pages])]
      .filter(value => value >= 1 && value <= pages);
    shortcuts.innerHTML = choices.map(value => '<button type="button" data-page="' + value +
      '" aria-label="' + t('Página') + ' ' + fmt.format(value) + '"' +
      (value === page ? ' aria-current="page"' : '') + '>' + fmt.format(value) + '</button>').join('');
    return pages;
  };
}

function renderActivityPeak(data) {
  if (!data.totals.total) {
    $('activity-peak').innerHTML = empty();
    return;
  }
  const bytes = row => row.up + row.down;
  const peak = data.series.reduce((highest, row) => bytes(row) > bytes(highest) ? row : highest);
  const total = bytes(peak);
  const step = data.series[1] ? data.series[1].at - data.series[0].at : data.end - peak.at;
  const start = stamp(peak.at);
  const stop = stamp(Math.min(data.end, peak.at + step));
  const range = start + ' → ' + (start.slice(0, 10) === stop.slice(0, 10) ? stop.slice(11) : stop);
  const share = fmt.format(total * 100 / data.totals.total);
  $('activity-peak').innerHTML = '<div class="activity-volume"><b>' + volume(total) +
    '</b><small>' + share + '% · ' + t('do volume do período') + '</small></div>' +
    '<p class="activity-time">' + range + ' UTC</p>' +
    '<div class="service-bars activity-bar" aria-hidden="true">' +
    '<i class="sent" style="width:' + (100 * peak.up / total) + '%"></i>' +
    '<i class="received" style="width:' + (100 * peak.down / total) + '%"></i></div>' +
    '<div class="activity-directions"><span class="up">' + t('Enviado') + ' ' + volume(peak.up) +
    '</span><span class="down">' + t('Recebido') + ' ' + volume(peak.down) + '</span></div>' +
    '<p class="hint">' + t('Intervalo pela hora de recepção dos registos.') + '</p>';
}

function renderServices(rows, total) {
  if (!rows.length) {
    $('port').innerHTML = empty();
    return;
  }
  const peak = Math.max(1, rows[0].total);
  const share = value => fmt.format(total > 0 ? value * 100 / total : 0) + '%';
  const featured = rows.reduce((sum, row) => sum + row.total, 0);
  const rest = Math.max(0, total - featured);
  const segments = rows.map((row, i) => {
    const label = [row.key, row.service].filter(Boolean).join(' · ');
    return '<span style="--segment:' + colors[i] + ';flex-grow:' + row.total +
      '" title="' + esc(label) + ' · ' + share(row.total) + '"></span>';
  });
  if (rest) segments.push('<span class="remainder" style="flex-grow:' + rest +
    '" title="' + t('Restante tráfego') + ' · ' + share(rest) + '"></span>');
  const details = rows.map((row, i) => {
    const breakdown = t('Enviado') + ' ' + volume(row.up) + ' · ' +
      t('Recebido') + ' ' + volume(row.down);
    return '<button class="service-row" type="button" data-query="port:' + esc(row.key) + '">' +
      '<span class="service-name"><i style="--segment:' + colors[i] + '"></i>' +
      '<span><b>' + esc(row.service || row.key) + '</b>' +
      (row.service ? '<small>' + esc(row.key) + '</small>' : '') + '</span></span>' +
      '<span class="service-volume"><span class="service-bars" aria-hidden="true">' +
      '<i class="sent" style="width:' + (100 * row.up / peak) + '%"></i>' +
      '<i class="received" style="width:' + (100 * row.down / peak) + '%"></i></span>' +
      '<small>' + breakdown + '</small></span>' +
      '<span class="service-total"><b>' + volume(row.total) + '</b>' +
      '<small>' + share(row.total) + '</small></span></button>';
  }).join('');
  $('port').innerHTML = '<div class="service-share"><span>' +
    t('Quota do volume total') + '</span><b>' + share(featured) + '</b></div>' +
    '<div class="service-segments" aria-hidden="true">' + segments.join('') + '</div>' +
    '<div class="service-share"><small>' + t('Portos apresentados') + ' · ' +
    volume(featured) + '</small><small>' + t('Restante tráfego') + ' · ' + volume(rest) +
    '</small></div><div class="service-list">' + details + '</div>';
}

let mapState, mapRequest, mapPicker;

function clearMap() {
  mapRequest?.abort();
  mapState = null;
  renderMap();
}

function clearArchiveMap() {
  if (mapState?.query.has('archive')) clearMap();
}

function mapEndpoint(ip, port) {
  return ip ? esc(port > 0 ? ip + ':' + port : ip) : '—';
}

function mapNode(label, value, detail = '') {
  return '<div class="map-node"><small>' + t(label) + '</small><b>' + value +
    '</b>' + (detail ? '<small>' + detail + '</small>' : '') + '</div>';
}

function renderFlowMap(flow) {
  const asn = value => value > 0 ? 'AS' + esc(value) : '';
  const interfaces = [
    flow.in_if > 0 ? t('Entrada') + ' #' + esc(flow.in_if) : '',
    flow.out_if > 0 ? t('Saída') + ' #' + esc(flow.out_if) : '',
  ].filter(Boolean).join(' · ');
  const nodes = [
    mapNode('Origem', mapEndpoint(flow.src, flow.sport), asn(flow.sasn)),
    mapNode('Exportador NetFlow', esc(flow.router), interfaces),
  ];
  if (flow.next_hop && ![flow.src, flow.router, flow.dst].includes(flow.next_hop)) {
    nodes.push(mapNode('Próximo salto exportado', esc(flow.next_hop)));
  }
  nodes.push(mapNode('Destino', mapEndpoint(flow.dst, flow.dport), asn(flow.dasn)));
  const details = [
    ['Protocolo / serviço', esc([t(flow.proto), flow.service].filter(Boolean).join(' · '))],
    ['Volume total', volume(flow.bytes)],
    ['Pacotes', fmt.format(flow.packets)],
    ['Sentido', t(flow.direction === 'up' ? 'Enviado' : 'Recebido')],
    ['Recepção UTC', stamp(flow.received)],
  ];
  if (flow.started > 0) details.push(['Início UTC', stamp(flow.started / 1e9)]);
  if (flow.ended > 0) details.push(['Fim UTC', stamp(flow.ended / 1e9)]);
  if (flow.next_hop) details.push(['Próximo salto exportado', esc(flow.next_hop)]);
  if (flow.bgp_next_hop) details.push(['Próximo salto BGP', esc(flow.bgp_next_hop)]);
  const fields = details.map(([key, value]) =>
    '<div><dt>' + t(key) + '</dt><dd>' + value + '</dd></div>').join('');
  return '<p class="hint">' + t('Os troços tracejados representam segmentos sem visibilidade no NetFlow.') +
    '</p><div class="flow-path">' + nodes.join('<i class="map-gap" aria-hidden="true"></i>') +
    '</div><dl class="map-fields">' + fields + '</dl>';
}

function renderPeerMap(data) {
  if (!data.rows.length) return empty();
  const caption = fmt.format(data.matches) + ' ' + t(data.host ? 'pares' : 'destinos') +
    ' · ' + volume(data.total);
  const root = mapNode(data.host ? 'Host' : 'Rede de origem', esc(data.target), caption);
  const leaves = data.rows.map(row => {
    const thickness = 2 + 12 * Math.sqrt(row.total / Math.max(1, data.peak));
    const breakdown = data.host
      ? '→ ' + volume(row.from_host) + ' · ← ' + volume(row.to_host)
      : fmt.format(row.flows) + ' ' + t(row.flows === 1 ? 'registo' : 'registos');
    return '<li><i class="map-branch" aria-hidden="true" style="--weight:' + thickness +
      'px"></i><div class="map-node"><b>' + esc(row.peer) +
      '</b><small>' + breakdown + '</small><strong>' + volume(row.total) + '</strong></div></li>';
  }).join('');
  const hint = data.host
    ? t('→ Do host para o par · ← Do par para o host')
    : t('Da rede de origem para cada destino observado');
  return '<p class="hint">' + hint + ' · ' + t('Espessura por volume') +
    '</p><div class="peer-map">' + root + '<ul>' + leaves + '</ul></div>';
}

function renderMap() {
  const data = mapState?.data;
  const query = mapState?.query;
  const archiveMode = query ? query.has('archive') : dataset === 'archive';
  $('map-source').textContent = (archiveMode ? t('Arquivo UTC') + ' · ' + archive.date : t('Em directo')) +
    ' · ' + t('Usa os filtros do painel');
  $('map-title').textContent = t(mapState?.kind === 'flow' ? 'Fluxo observado'
    : data?.host ? 'Pares do host' : data ? 'Destinos da rede' : 'Mapa de tráfego');
  $('map-content').innerHTML = data
    ? (mapState.kind === 'flow' ? renderFlowMap(data) : renderPeerMap(data))
    : '<p class="empty">' + esc(t(mapState?.message ||
      'Escolhe um fluxo no painel ou indica um host ou uma rede.')) + '</p>';
  const pages = mapPicker(data?.size ? data : null);
  $('map-pages').hidden = pages <= 1;
  if (pages > 1) {
    $('map-prev').disabled = data.page <= 1;
    $('map-next').disabled = data.page >= pages;
  }
}

async function loadMap(kind, query) {
  mapRequest?.abort();
  const controller = new AbortController();
  mapRequest = controller;
  mapState = {kind, query, message:'A consultar o SQLite…'};
  renderMap();
  try {
    const response = await fetch('/api/view?' + query, {signal:controller.signal});
    const data = await response.json();
    if (!response.ok) throw Error(data.error);
    if (controller.signal.aborted) return;
    if (data.size) {
      if (data.page > 1 && !data.rows.length) {
        query.set('page', 1);
        return loadMap(kind, query);
      }
      query.set('end', data.end);
      query.set('ceiling', data.ceiling);
    }
    mapState.data = data;
  } catch (error) {
    if (!controller.signal.aborted) mapState.message = requestError(error);
  } finally {
    if (!controller.signal.aborted) renderMap();
  }
}

function initMaps() {
  $('map-form').onsubmit = event => {
    event.preventDefault();
    const query = params(1, false);
    // A newly requested live map can outlive the dashboard's snapshot.
    if (!query.has('archive') && Date.now() / 1000 - Number(query.get('end')) > 600) {
      query.delete('end');
      query.delete('ceiling');
    }
    query.set('network', $('map-form').elements.target.value.trim());
    loadMap('peer', query);
  };
  const jump = page => {
    const query = new URLSearchParams(mapState.query);
    query.set('page', page);
    loadMap('peer', query);
  };
  mapPicker = makePagePicker('map-page', jump);
  for (const [id, delta] of [['map-prev', -1], ['map-next', 1]]) {
    $(id).onclick = () => jump(mapState.data.page + delta);
  }
}
