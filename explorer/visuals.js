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

function renderServices(rows) {
  if (!rows.length) {
    $('port').innerHTML = empty();
    return;
  }
  const peak = Math.max(1, rows[0].flows);
  const details = rows.map(row => {
    const breakdown = t('Enviado') + ' ' + fmt.format(row.up_flows) + ' · ' +
      t('Recebido') + ' ' + fmt.format(row.down_flows);
    return '<button class="service-row" type="button" data-query="port:' + esc(row.key) + '">' +
      '<span class="service-name"><b>' + esc(row.service || row.key) + '</b>' +
      (row.service ? '<small>' + esc(row.key) + '</small>' : '') + '</span>' +
      '<span class="service-volume"><span class="service-bars" aria-hidden="true">' +
      '<i class="sent" style="width:' + (100 * row.up_flows / peak) + '%"></i>' +
      '<i class="received" style="width:' + (100 * row.down_flows / peak) + '%"></i></span>' +
      '<small>' + breakdown + '</small></span>' +
      '<span class="service-total"><b>' + fmt.format(row.flows) + '</b>' +
      '<small>' + volume(row.total) + '</small></span></button>';
  }).join('');
  $('port').innerHTML = '<div class="service-list">' + details + '</div>';
}

function applicationText(row) {
  if (row.application_status === 'named') return row.application_name;
  if (row.application_status === 'unknown') return t('Não identificada');
  if (row.application_status === 'unmapped') return t('Sem nome');
  return '';
}

function renderApplications(charts) {
  const noApps = '<p class="empty">' + t('Sem aplicações NBAR identificadas neste intervalo.') + '</p>';
  $('app-count').innerHTML = charts.app_count.length ? barList(charts.app_count, 'nbar', 'flows') : noApps;
  $('app-volume').innerHTML = charts.app_volume.length ? barList(charts.app_volume, 'nbar') : noApps;
  $('app-pie').innerHTML = '';
  if (!charts.app_total) return;

  const colors = ['#38d8b0', '#8da8ff', '#ffcb78', '#f38cb1', '#72caf5', '#65768e'];
  const slices = charts.app_volume.slice(0, 5).filter(row => row.total > 0)
    .map((row, index) => ({...row, color: colors[index]}));
  const rest = charts.app_total - slices.reduce((sum, row) => sum + row.total, 0);
  if (rest > 0) slices.push({key: t('Outras aplicações'), total: rest, color: colors[5]});
  let angle = 0;
  const segments = [];
  const legend = slices.map((row, index) => {
    const share = row.total / charts.app_total;
    const end = index === slices.length - 1 ? 360 : angle + share * 360;
    segments.push(`${row.color} ${angle}deg ${end}deg`);
    angle = end;
    return `<li><i style="background:${row.color}" aria-hidden="true"></i><span>${esc(row.key)}</span><b>${fmt.format(share * 100)}%</b></li>`;
  }).join('');
  $('app-pie').innerHTML = `<figure class="app-pie"><div class="app-donut" style="background:conic-gradient(${segments.join(',')})" aria-hidden="true"></div>
    <figcaption><b>${volume(charts.app_total)} · ${t('Volume NBAR identificado')}</b><ul>${legend}</ul></figcaption></figure>`;
}

let mapState, mapRequest;

function clearMap() {
  mapRequest?.abort();
  mapState = null;
  renderMap();
}

function clearArchiveMap() {
  if (mapState?.query.has('archive')) clearMap();
}

function mapEndpoint(ip, port) {
  return ip ? ipSearchButton(ip, port > 0 ? ip + ':' + port : ip) : '—';
}

function mapNode(label, value, detail = '', role = '') {
  return '<li class="map-node ' + role + '"><span class="map-marker" aria-hidden="true"></span>' +
    '<div><small>' + t(label) + '</small><b>' + value +
    '</b>' + (detail ? '<small>' + detail + '</small>' : '') + '</div></li>';
}

function renderFlowMap(flow) {
  const asn = value => value > 0 ? 'AS' + esc(value) : '';
  const interfaces = [
    flow.in_if > 0 ? t('Entrada') + ' #' + esc(flow.in_if) : '',
    flow.out_if > 0 ? t('Saída') + ' #' + esc(flow.out_if) : '',
  ].filter(Boolean).join(' · ');
  const nodes = [
    mapNode('Origem', mapEndpoint(flow.src, flow.sport), asn(flow.sasn), 'map-origin'),
    mapNode('Exportador NetFlow', mapEndpoint(flow.router), interfaces, 'map-exporter'),
  ];
  if (flow.next_hop && ![flow.src, flow.router, flow.dst].includes(flow.next_hop)) {
    nodes.push(mapNode('Próximo salto exportado', mapEndpoint(flow.next_hop)));
  }
  nodes.push(mapNode('Destino', mapEndpoint(flow.dst, flow.dport), asn(flow.dasn), 'map-destination'));
  const details = [
    ['Protocolo / serviço', esc([t(flow.proto), flow.service].filter(Boolean).join(' · '))],
    ['Volume total', volume(flow.bytes)],
    ['Pacotes', fmt.format(flow.packets)],
    ['Sentido', t(flow.direction === 'up' ? 'Enviado' : 'Recebido')],
    ['Recepção UTC', stamp(flow.received)],
  ];
  const app = applicationText(flow);
  if (app) details.push(['Aplicação NBAR', esc(app)]);
  if (flow.started > 0) details.push(['Início UTC', stamp(flow.started / 1e9)]);
  if (flow.ended > 0) details.push(['Fim UTC', stamp(flow.ended / 1e9)]);
  if (flow.next_hop) details.push(['Próximo salto exportado', esc(flow.next_hop)]);
  if (flow.bgp_next_hop) details.push(['Próximo salto BGP', esc(flow.bgp_next_hop)]);
  const fields = details.map(([key, value]) =>
    '<div><dt>' + t(key) + '</dt><dd>' + value + '</dd></div>').join('');
  return '<ol class="flow-path" style="--nodes:' + nodes.length + '">' + nodes.join('') +
    '</ol><p class="map-note"><i aria-hidden="true"></i>' +
    t('Os troços tracejados representam segmentos sem visibilidade no NetFlow.') +
    '</p><dl class="map-fields">' + fields + '</dl>';
}

function renderPeerMap(data) {
  if (!data.rows.length) return empty();
  const prefix = data.aggregation_prefix;
  const count = fmt.format(data.rows.length) + ' ' +
    t(prefix ? 'redes' : data.host ? 'pares' : 'destinos');
  const root = '<div class="peer-hub"><small>' + t(data.host ? 'Host' : 'Rede de origem') +
    '</small><b>' + (data.host ? ipSearchButton(data.target) : esc(data.target)) +
    '</b><strong>' + volume(data.total) + '</strong></div>';
  const step = 62;
  const height = step * data.rows.length;
  const curves = data.rows.map((row, index) => {
    const y = step * (index + .5);
    const width = 1.5 + 6 * Math.sqrt(row.total / Math.max(1, data.peak));
    return '<path d="M0 ' + height / 2 + ' C45 ' + height / 2 + ' 45 ' + y +
      ' 100 ' + y + '" stroke-width="' + width + '"/>';
  }).join('');
  const leaves = data.rows.map(row => {
    const breakdown = data.host
      ? '<span class="up">→ ' + volume(row.from_host) + '</span> · <span class="down">← ' +
        volume(row.to_host) + '</span>'
      : fmt.format(row.flows) + ' ' + t(row.flows === 1 ? 'registo' : 'registos');
    return '<li><div class="peer-leaf"><span class="peer-dot" aria-hidden="true"></span>' +
      '<div><b>' + (prefix ? esc(row.peer) : ipSearchButton(row.peer)) +
      '</b><small>' + breakdown + '</small></div><strong>' + volume(row.total) + '</strong></div></li>';
  }).join('');
  const hint = data.host
    ? t('→ Do host para o par · ← Do par para o host')
    : t('Da rede de origem para cada destino observado');
  const aggregation = prefix ? ' · ' + t('Agregação automática') + ' /' + prefix : '';
  return '<div class="map-meta"><span>' + count + aggregation +
    '</span><span>' + t('Espessura por volume') + '</span></div>' +
    '<div class="peer-map">' + root + '<div class="peer-fan">' +
    '<svg viewBox="0 0 100 ' + height + '" preserveAspectRatio="none" aria-hidden="true">' +
    curves + '</svg><ul>' + leaves + '</ul></div></div><p class="map-note">' + hint + '</p>';
}

function renderMap() {
  const data = mapState?.data;
  const query = mapState?.query;
  const archiveMode = query ? query.has('archive') : dataset === 'archive';
  $('map-source').textContent = (archiveMode ? t('Arquivo UTC') + ' · ' + archive.date : t('Em directo')) +
    (mapState?.kind === 'flow' ? '' : ' · ' + t('Usa os filtros do painel'));
  $('map-title').textContent = t(mapState?.kind === 'flow' ? 'Fluxo observado'
    : data?.host ? 'Pares do host' : data ? 'Destinos da rede' : 'Mapa de tráfego');
  $('map-content').innerHTML = data
    ? (mapState.kind === 'flow' ? renderFlowMap(data) : renderPeerMap(data))
    : '<p class="empty">' + esc(t(mapState?.message ||
      'Escolhe um fluxo no painel ou indica um host ou uma rede.')) + '</p>';
}

async function loadMap(kind, query) {
  mapRequest?.abort();
  const controller = new AbortController();
  mapRequest = controller;
  mapState = {kind, query, message:'A carregar o mapa…'};
  renderMap();
  try {
    const data = await viewResponse(query, controller.signal);
    if (controller.signal.aborted) return;
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
}
