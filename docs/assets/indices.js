/* Shared chart for monthly and quarterly price and cost indices (metals and economics pages).
   Every series is rebased to its 2021 average = 100, so lines from different publishers and
   base years start from the same point. Needs d3 and the page's CSS (.hero, .seg, .chart, ...). */
(function () {
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#888';
  const fmt1 = d3.format(',.1f');
  const pct = (v) => (v == null || !isFinite(v) ? '–' : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(1)}%`);
  const cls = (v) => (v == null || !isFinite(v) || Math.abs(v) < 0.05 ? '' : v > 0 ? 'up' : 'down');
  const COLORS = ['--s1', '--s2', '--s3', '--s4', '--s5'];
  const DASH = { US: null, DE: '6 3', CN: '2 3' };
  const periodLabel = (iso, freq) => {
    const d = new Date(iso + 'T00:00:00Z');
    return freq === 'Q' ? `Q${Math.floor(d.getUTCMonth() / 3) + 1} ${d.getUTCFullYear()}` : d3.utcFormat('%b %Y')(d);
  };

  function rebase(s, rate) {
    const base = rate ? [] : Object.entries(s.data).filter(([d]) => d.startsWith('2021-')).map(([, v]) => v);
    const b = base.length ? d3.mean(base) : null;
    const pts = Object.entries(s.data).sort(([a], [b2]) => (a < b2 ? -1 : 1))
      .map(([d, v]) => ({ d, x: new Date(d + 'T00:00:00Z'), v: b ? (v / b) * 100 : v, raw: v }));
    return { ...s, pts, rebased: !!b };
  }

  function render(el, input, opts = {}) {
    const rate = !!opts.rate;          // rate mode: annual rates in %, shown as published (no rebasing)
    const series = input.filter((s) => s && Object.keys(s.data || {}).length).map((s, i) => ({
      ...rebase(s, rate), color: s.color || COLORS[i % COLORS.length], dash: s.dash !== undefined ? s.dash : (rate ? null : DASH[s.country] ?? null),
    }));
    const ref = rate ? 0 : 100;
    const vfmt = rate ? (v) => `${fmt1(v)}%` : fmt1;
    el.replaceChildren();
    if (!series.length) {
      el.innerHTML = '<p class="sub">No index data yet - it appears after the next daily update.</p>';
      return () => {};
    }
    const off = new Set(series.filter((s) => s.off).map((s) => s.key));
    const st = { range: opts.range || '10y' };
    el.innerHTML = `
      <div class="hero">
        <div class="controls"><div class="seg" role="group" aria-label="Period" data-range></div></div>
        <div class="chart" data-chart></div>
        <div class="legend" data-legend></div>
      </div>
      <p class="sub" style="margin-top:10px">${rate ? 'Change against the same month a year earlier, in %.' : 'Index, 2021 average = 100.'} ${opts.note || ''}</p>
      <div class="table-wrap"><table data-table></table></div>`;
    const q = (s) => el.querySelector(s);
    const ranges = [['5y', '5 years'], ['10y', '10 years'], ['all', 'Since 2000']];
    for (const [v, label] of ranges) {
      const b = document.createElement('button'); b.type = 'button'; b.textContent = label;
      b.setAttribute('aria-pressed', String(v === st.range));
      b.onclick = () => { st.range = v; q('[data-range]').querySelectorAll('button').forEach((x) => x.setAttribute('aria-pressed', String(x === b))); draw(); };
      q('[data-range]').append(b);
    }
    for (const s of series) {
      const b = document.createElement('button'); b.type = 'button'; b.className = 'toggle';
      b.setAttribute('aria-pressed', String(!off.has(s.key)));
      b.title = s.desc || '';
      b.innerHTML = `<span class="sw${s.dash ? ' dash' : ''}"></span>`;
      b.firstChild.style.background = s.dash ? 'none' : css(s.color);
      b.firstChild.style.color = css(s.color);
      b.append(s.label);
      b.onclick = () => { off.has(s.key) ? off.delete(s.key) : off.add(s.key); b.setAttribute('aria-pressed', String(!off.has(s.key))); draw(); };
      q('[data-legend]').append(b);
    }

    // latest values table
    const rows = rate ? series.map((s) => {
      const last = s.pts.at(-1), prev = s.pts.at(-2);
      const yearAgo = s.pts.find((p) => p.d === `${+last.d.slice(0, 4) - 1}${last.d.slice(4)}`);
      const avg12 = s.pts.length >= 12 ? d3.mean(s.pts.slice(-12), (p) => p.v) : null;
      const link = s.link ? `<a href="${s.link}">source</a>` : '';
      const sw = `<span class="sw${s.dash ? ' dash' : ''}" style="background:${s.dash ? 'none' : css(s.color)};color:${css(s.color)}"></span>`;
      return `<tr><td class="mkt">${sw}${s.label}<small>${s.desc || ''} ${link}</small></td><td>${periodLabel(last.d, s.freq)}</td>
        <td class="big">${vfmt(last.v)}</td><td>${prev ? vfmt(prev.v) : '–'}</td><td>${yearAgo ? vfmt(yearAgo.v) : '–'}</td><td>${avg12 != null ? vfmt(avg12) : '–'}</td></tr>`;
    }).join('') : series.map((s) => {
      const last = s.pts.at(-1);
      const yearAgo = s.pts.find((p) => p.d === `${+last.d.slice(0, 4) - 1}${last.d.slice(4)}`);
      const ch1y = yearAgo ? (last.raw / yearAgo.raw - 1) * 100 : null;
      const since21 = s.rebased ? last.v - 100 : null;
      const link = s.link ? `<a href="${s.link}">${s.source === 'fred' ? 'FRED ' + s.id : 'Eurostat'}</a>` : '';
      return `<tr><td class="mkt"><span class="sw${s.dash ? ' dash' : ''}" style="background:${s.dash ? 'none' : css(s.color)};color:${css(s.color)}"></span>${s.label}
        <small>${s.desc || ''}${s.adjusted ? ' Seasonally and calendar adjusted.' : ''} ${link}</small></td>
        <td>${periodLabel(last.d, s.freq)}</td><td class="num">${fmt1(last.v)}</td>
        <td class="num ${cls(ch1y)}">${pct(ch1y)}</td><td class="num ${cls(since21)}">${pct(since21)}</td></tr>`;
    }).join('');
    q('[data-table]').innerHTML = rate
      ? `<thead><tr><th>Country</th><th>Latest</th><th>Inflation</th><th>Month before</th><th>Year before</th><th>12-month average</th></tr></thead><tbody>${rows}</tbody>`
      : `<thead><tr><th>Index</th><th>Latest</th><th class="num">2021 = 100</th>
      <th class="num">1 year</th><th class="num">Since 2021</th></tr></thead><tbody>${rows}</tbody>`;

    function draw() {
      const box = q('[data-chart]'); box.replaceChildren();
      const W = Math.max(280, box.clientWidth), H = W < 560 ? 260 : 340, m = { t: 10, r: 14, b: 26, l: 44 };
      const shown = series.filter((s) => !off.has(s.key));
      const lastAll = d3.max(series, (s) => s.pts.at(-1).x);
      const from = st.range === 'all' ? new Date('2000-01-01T00:00:00Z')
        : d3.utcYear.offset(lastAll, st.range === '5y' ? -5 : -10);
      const vis = shown.map((s) => ({ ...s, v: s.pts.filter((p) => p.x >= from) }));
      const allPts = vis.flatMap((s) => s.v);
      const x = d3.scaleUtc().domain([d3.max([from, d3.min(allPts, (p) => p.x) || from]), lastAll]).range([m.l, W - m.r]);
      const ext = d3.extent(allPts, (p) => p.v);
      const y = d3.scaleLinear().domain(ext[0] == null ? [ref - 20, ref + 20] : [Math.min(ext[0], ref), Math.max(ext[1], ref)]).nice().range([H - m.b, m.t]);
      const svg = d3.select(box).append('svg').attr('width', W).attr('height', H);
      svg.append('g').attr('transform', `translate(0,${H - m.b})`).call(d3.axisBottom(x).ticks(W < 560 ? 4 : 8).tickSizeOuter(0));
      svg.append('g').attr('transform', `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(6).tickSize(-(W - m.l - m.r)).tickFormat(rate ? (v) => `${v}%` : null))
        .call((g) => g.select('.domain').remove());
      svg.append('line').attr('x1', m.l).attr('x2', W - m.r).attr('y1', y(ref)).attr('y2', y(ref))
        .attr('stroke', css('--ink-2')).attr('stroke-opacity', 0.5).attr('stroke-dasharray', '2 3');
      const line = d3.line().x((p) => x(p.x)).y((p) => y(p.v));
      for (const s of vis) {
        svg.append('path').datum(s.v).attr('fill', 'none').attr('stroke', css(s.color)).attr('stroke-width', 2)
          .attr('stroke-dasharray', s.dash).attr('d', line);
      }
      // hover: nearest period of each series
      const tip = document.createElement('div'); tip.className = 'tip'; tip.hidden = true; box.append(tip);
      const rule = svg.append('line').attr('y1', m.t).attr('y2', H - m.b).attr('stroke', css('--ink-2')).attr('opacity', 0);
      svg.append('rect').attr('x', m.l).attr('y', m.t).attr('width', W - m.l - m.r).attr('height', H - m.t - m.b)
        .attr('fill', 'transparent')
        .on('pointermove', (ev) => {
          const t = x.invert(d3.pointer(ev)[0]);
          const near = vis.map((s) => ({ s, p: s.v.length ? s.v[Math.max(0, d3.bisector((p) => p.x).right(s.v, t) - 1)] : null })).filter((n) => n.p);
          if (!near.length) return;
          const ref = near.reduce((a, b) => (Math.abs(b.p.x - t) < Math.abs(a.p.x - t) ? b : a));
          rule.attr('x1', x(ref.p.x)).attr('x2', x(ref.p.x)).attr('opacity', 0.4);
          tip.innerHTML = `<div class="tip-grid">${near.map((n) => `<span><span class="sw${n.s.dash ? ' dash' : ''}" style="background:${n.s.dash ? 'none' : css(n.s.color)};color:${css(n.s.color)}"></span>${n.s.label} <span class="tip-date">${periodLabel(n.p.d, n.s.freq)}</span></span><span class="v">${vfmt(n.p.v)}</span>`).join('')}</div>`;
          tip.hidden = false;
          const px = x(ref.p.x);
          tip.style.left = `${px > W / 2 ? px - tip.offsetWidth - 12 : px + 12}px`;
        })
        .on('pointerleave', () => { tip.hidden = true; rule.attr('opacity', 0); });
    }
    draw();
    return draw;
  }

  window.IndexPanel = { render };
})();
