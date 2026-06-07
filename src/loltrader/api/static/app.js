/* LoL Trader v3 — vanilla JS dashboard
 *
 * Single-page app:
 *   - Fetches market list from /api/markets
 *   - Connects to /ws for real-time ticker + orderbook updates
 *   - Renders market cards with one-click BUY YES / BUY NO
 *   - Updates orderbook displays in real-time without re-rendering
 */

// ----- State -----
const state = {
    ws: null,
    markets: [],            // array of market objects
    eventGroups: {},        // event_ticker -> [markets]
    games: [],              // live games from /api/games
    gameByTeam: {},         // team_code -> game (built from games[])
    predictions: {},        // market_ticker -> MarketPrediction
    subscribed: new Set(),  // set of market_tickers we've subscribed to via WS
    expandedBooks: new Set(),
    liveMode: false,
    league: '',
    defaultContracts: 50,
    limitPadding: 10,
    lastTickTs: 0,
};

const elements = {
    indicator: document.getElementById('ws-indicator'),
    wsText: document.getElementById('ws-text'),
    marketCount: document.getElementById('market-count'),
    tickTime: document.getElementById('tick-time'),
    liveToggle: document.getElementById('live-mode-toggle'),
    modeLabel: document.getElementById('mode-label'),
    leagueFilter: document.getElementById('league-filter'),
    defaultContracts: document.getElementById('default-contracts'),
    limitPadding: document.getElementById('limit-padding'),
    refreshBtn: document.getElementById('refresh-btn'),
    container: document.getElementById('markets-container'),
};

// ----- Toast notifications -----
function ensureToastContainer() {
    let c = document.getElementById('toast-container');
    if (!c) {
        c = document.createElement('div');
        c.id = 'toast-container';
        document.body.appendChild(c);
    }
    return c;
}

function toast(message, kind = 'info', timeoutMs = 4000) {
    const container = ensureToastContainer();
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => {
        el.style.opacity = '0';
        el.style.transition = 'opacity 0.3s';
        setTimeout(() => el.remove(), 300);
    }, timeoutMs);
}

// ----- WebSocket -----
function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${location.host}/ws`;
    const ws = new WebSocket(url);
    state.ws = ws;

    setIndicator('connecting', 'connecting…');

    ws.addEventListener('open', () => {
        setIndicator('connected', 'live');
        // Subscribe to currently-displayed markets
        if (state.markets.length > 0) {
            const tickers = state.markets.map(m => m.market_ticker);
            ws.send(JSON.stringify({ type: 'subscribe', tickers }));
            state.subscribed = new Set(tickers);
        }
    });

    ws.addEventListener('close', () => {
        setIndicator('disconnected', 'reconnecting…');
        setTimeout(connectWS, 2000);
    });

    ws.addEventListener('error', () => {
        // Let close handler do the reconnect
    });

    ws.addEventListener('message', (event) => {
        let msg;
        try { msg = JSON.parse(event.data); } catch (e) { return; }
        handleWsMessage(msg);
    });
}

function setIndicator(status, text) {
    elements.indicator.className = `indicator ${status}`;
    elements.wsText.textContent = text;
}

function handleWsMessage(msg) {
    state.lastTickTs = msg.ts || Date.now();
    elements.tickTime.textContent = new Date(state.lastTickTs).toLocaleTimeString();

    if (msg.type === 'ticker') {
        updateTickerInUI(msg);
    } else if (msg.type === 'orderbook') {
        updateOrderbookInUI(msg);
    } else if (msg.type === 'game_frame') {
        updateGameFrameInUI(msg.frame);
    } else if (msg.type === 'winprob_update') {
        updateWinprobInUI(msg.prediction);
    }
}

// --- Phase 5: live win-prob rendering -----------------------------------

// Minimum edge (in cents) to highlight a market card as "actionable"
const EDGE_HIGHLIGHT_CENTS = 5;
// Maximum ensemble band-width (in cents) above which a prediction is treated
// as too uncertain to flag
const MAX_BAND_FOR_ALERT = 20;

if (!state.winprobByGameId) state.winprobByGameId = {};

function updateWinprobInUI(pred) {
    // Cache the latest prediction by game_id
    state.winprobByGameId[pred.game_id] = pred;

    // Find market cards whose teams match this game
    const teamCodes = [pred.blue_team_code, pred.red_team_code].filter(Boolean);
    if (teamCodes.length === 0) return;

    for (const card of document.querySelectorAll('.market-card')) {
        const codes = [...card.querySelectorAll('.team-label')].map(e => e.textContent);
        if (!codes.some(c => teamCodes.includes(c))) continue;

        // Determine which side this market's YES contract resolves to. We try
        // to read it from a data-side attribute (set by the card renderer);
        // fall back to assuming YES = blue team for v1.
        const yesIsBlue = card.dataset.yesSide
            ? card.dataset.yesSide === 'blue'
            : true;
        const modelP = yesIsBlue ? pred.p_blue : (1.0 - pred.p_blue);
        const bandPct = Math.round(pred.band_width * 100);

        // Pull current market ask (YES) from the card to compute edge
        const askEl = card.querySelector('.ask-val');
        const askText = askEl ? askEl.textContent.replace('¢', '').trim() : '';
        const marketAsk = parseInt(askText, 10);  // NaN if no ask yet

        const modelPct = Math.round(modelP * 100);
        const edgeCents = Number.isFinite(marketAsk) ? (modelPct - marketAsk) : null;

        renderEdgeStrip(card, modelPct, bandPct, marketAsk, edgeCents, pred);
    }
}

function renderEdgeStrip(card, modelPct, bandPct, marketAsk, edgeCents, pred) {
    let strip = card.querySelector('.winprob-strip');
    if (!strip) {
        strip = document.createElement('div');
        strip.className = 'winprob-strip';
        const header = card.querySelector('.market-header');
        const gameStrip = card.querySelector('.game-strip');
        // Insert AFTER game-strip if present, otherwise after header
        (gameStrip || header).after(strip);
    }

    const isStale = !pred.has_full_features;
    const bandTag = isStale
        ? '<span class="winprob-stale" title="picks not resolved — features incomplete">(degraded)</span>'
        : '';

    let edgeHtml = '<span class="winprob-edge-na">no ask</span>';
    let alertClass = '';
    if (edgeCents !== null) {
        const sign = edgeCents >= 0 ? '+' : '';
        edgeHtml = `<span class="winprob-edge edge-${edgeCents >= 0 ? 'pos' : 'neg'}">edge ${sign}${edgeCents}¢</span>`;
        if (Math.abs(edgeCents) >= EDGE_HIGHLIGHT_CENTS && bandPct <= MAX_BAND_FOR_ALERT) {
            alertClass = ' winprob-alert';
        }
    }

    strip.className = `winprob-strip${alertClass}`;
    strip.innerHTML = `
        <span class="winprob-model">model: <b>${modelPct}¢</b> <span class="band">±${(bandPct/2).toFixed(0)}¢</span></span>
        <span class="winprob-mkt">mkt: ${Number.isFinite(marketAsk) ? marketAsk + '¢' : '—'}</span>
        ${edgeHtml}
        ${bandTag}
        <span class="winprob-min">m${pred.minute}</span>
    `;
}

function updateGameFrameInUI(frame) {
    // Update the in-memory game cache so that re-rendering picks it up
    const teamCodes = [frame.blue_team_code, frame.red_team_code].filter(Boolean);
    // Merge into state.games and gameByTeam by game_id
    const idx = state.games.findIndex(g => g.game_id === frame.game_id);
    if (idx >= 0) state.games[idx] = { ...state.games[idx], ...frame };
    else state.games.push(frame);
    for (const t of teamCodes) state.gameByTeam[t] = frame;

    // Update the live strip in-place without re-rendering the whole card
    for (const card of document.querySelectorAll('.market-card')) {
        const codes = [...card.querySelectorAll('.team-label')].map(e => e.textContent);
        if (!codes.some(c => teamCodes.includes(c))) continue;
        const existing = card.querySelector('.game-strip');
        const fresh = renderGameStrip(frame);
        if (existing) existing.replaceWith(fresh);
        else {
            // Insert after header
            const header = card.querySelector('.market-header');
            if (header) header.after(fresh);
        }
    }
}

function updateTickerInUI(msg) {
    // Update bid/ask spans for this market
    const ticker = msg.market_ticker;
    const card = document.querySelector(`[data-ticker="${ticker}"]`);
    if (!card) return;
    const bidEl = card.querySelector('.bid-val');
    const askEl = card.querySelector('.ask-val');
    const yesBtn = card.querySelector('.btn-buy-yes');
    const noBtn = card.querySelector('.btn-buy-no');

    if (bidEl && msg.yes_bid != null) bidEl.textContent = `${msg.yes_bid}¢`;
    if (askEl && msg.yes_ask != null) askEl.textContent = `${msg.yes_ask}¢`;
    if (yesBtn && msg.yes_ask) {
        yesBtn.textContent = `BUY YES @ ${msg.yes_ask}¢`;
        yesBtn.dataset.price = msg.yes_ask;
    }
    if (noBtn && msg.yes_bid != null) {
        const noPrice = 100 - msg.yes_bid;
        noBtn.textContent = `BUY NO @ ${noPrice}¢`;
        noBtn.dataset.price = noPrice;
    }

    // Flash the row to show activity
    card.style.transition = 'border-color 0.1s';
    card.style.borderColor = 'var(--accent-blue)';
    setTimeout(() => { card.style.borderColor = ''; }, 200);
}

function updateOrderbookInUI(msg) {
    const ticker = msg.market_ticker;
    if (!state.expandedBooks.has(ticker)) return;  // skip rendering closed books
    const book = document.querySelector(`[data-book-ticker="${ticker}"]`);
    if (!book) return;
    renderBook(book, msg);
}

function renderBook(bookEl, data) {
    const asks = (data.asks || []).slice(0, 5);
    const bids = (data.bids || []).slice(0, 5);
    const spread = (asks[0] && bids[0]) ? (asks[0][0] - bids[0][0]) : 0;

    // ASKS: render top-down (worst to best)
    let asksHtml = `<div class="book-side-header">ASKS (you pay to buy YES)</div>`;
    let cumAsk = 0;
    const reversedAsks = [...asks].reverse();
    for (const [p, s] of reversedAsks) {
        const cum = asks.filter(([pp]) => pp <= p).reduce((acc, [pp, ss]) => acc + pp * ss, 0);
        asksHtml += `<div class="book-row"><span class="price ask">${p}¢</span><span class="size">${s.toLocaleString()}</span><span class="cum">$${(cum/100).toFixed(0)}</span></div>`;
    }

    // Mid
    const age = Math.floor((Date.now() - (data.ts || Date.now())) / 1000);
    asksHtml += `<div class="book-meta">spread ${spread}¢ · updated ${age}s ago</div>`;

    // BIDS: top-down (best first)
    asksHtml += `<div class="book-side-header">BIDS (you receive selling YES)</div>`;
    for (const [p, s] of bids) {
        const cum = bids.filter(([pp]) => pp >= p).reduce((acc, [pp, ss]) => acc + pp * ss, 0);
        asksHtml += `<div class="book-row"><span class="price bid">${p}¢</span><span class="size">${s.toLocaleString()}</span><span class="cum">$${(cum/100).toFixed(0)}</span></div>`;
    }

    bookEl.innerHTML = asksHtml;
}

// ----- REST: fetch trades + P&L -----
function fmtCents(c) {
    if (c == null) return '—';
    const sign = c < 0 ? '-' : '';
    return `${sign}$${(Math.abs(c) / 100).toFixed(2)}`;
}

function pnlClass(c) {
    if (c == null) return 'pending';
    if (c > 0) return 'pos';
    if (c < 0) return 'neg';
    return '';
}

async function fetchTrades() {
    try {
        const r = await fetch('/api/trades?limit=20');
        if (!r.ok) return;
        const data = await r.json();
        renderPnLSummary(data.summary || {});
        renderTrades(data.trades || []);
    } catch (e) {
        // non-fatal
    }
}

function renderPnLSummary(s) {
    const realized = document.getElementById('pnl-realized');
    const unrealized = document.getElementById('pnl-unrealized');
    const total = document.getElementById('pnl-total');
    if (realized) {
        realized.textContent = fmtCents(s.realized_cents ?? 0);
        realized.className = `pnl-val ${pnlClass(s.realized_cents ?? 0)}`;
    }
    if (unrealized) {
        unrealized.textContent = fmtCents(s.unrealized_cents ?? 0);
        unrealized.className = `pnl-val ${pnlClass(s.unrealized_cents ?? 0)}`;
    }
    if (total) {
        total.textContent = fmtCents(s.total_cents ?? 0);
        total.className = `pnl-val pnl-total ${pnlClass(s.total_cents ?? 0)}`;
    }
}

function renderTrades(trades) {
    const list = document.getElementById('trades-list');
    if (!list) return;
    if (trades.length === 0) {
        list.innerHTML = '<div class="empty-state">No trades yet.</div>';
        return;
    }
    list.innerHTML = '';
    for (const t of trades) {
        const row = document.createElement('div');
        row.className = 'trade-row';
        const time = new Date(t.opened_at * 1000).toLocaleTimeString();
        const sideClass = t.side === 'YES' ? 'trade-side-yes' : 'trade-side-no';
        const modeClass = t.made_by === 'live' ? 'trade-mode-live' : 'trade-mode-paper';
        const modeLabel = t.made_by === 'live' ? '🔴 LIVE' : '📝 PAPER';
        const pnlCls = pnlClass(t.pnl);
        const pnlStr = t.pnl == null ? '—' : fmtCents(t.pnl);
        const team = String(t.market_ticker || '').split('-').pop();
        row.innerHTML = `
            <span class="trade-time">${time}</span>
            <span class="${sideClass}">${t.side} ×${t.contracts}</span>
            <span class="trade-fill">@ ${t.fill_price_cents}¢</span>
            <span class="trade-ticker" title="${escapeHtml(t.market_ticker || '')}">${escapeHtml(team)} · ${escapeHtml((t.market_title || '').slice(0, 40))}</span>
            <span class="${modeClass}">${modeLabel}</span>
            <span class="trade-pnl ${pnlCls}">${pnlStr} <span class="trade-pnl-kind">${t.pnl_kind}</span></span>
        `;
        list.appendChild(row);
    }
}

// ----- REST: fetch model predictions -----
async function fetchPredictions() {
    try {
        const r = await fetch('/api/predictions');
        if (!r.ok) return;
        const data = await r.json();
        state.predictions = {};
        for (const p of data.predictions || []) {
            state.predictions[p.market_ticker] = p;
        }
    } catch (e) {
        // non-fatal — model column just won't appear
    }
}

// ----- REST: fetch live games -----
async function fetchGames() {
    try {
        const r = await fetch('/api/games');
        if (!r.ok) return;
        const data = await r.json();
        state.games = data.games || [];
        state.gameByTeam = {};
        for (const g of state.games) {
            if (g.blue_team_code) state.gameByTeam[g.blue_team_code] = g;
            if (g.red_team_code) state.gameByTeam[g.red_team_code] = g;
        }
    } catch (e) {
        // non-fatal: game cards just won't appear
    }
}

// ----- REST: fetch markets list -----
async function fetchMarkets() {
    const params = new URLSearchParams();
    if (state.league) params.set('league', state.league);
    params.set('limit', '60');
    const r = await fetch(`/api/markets?${params}`);
    if (!r.ok) {
        toast(`Failed to fetch markets: ${r.status}`, 'error');
        return;
    }
    const data = await r.json();
    state.markets = data.markets;
    groupByEvent();
    renderMarkets();
    elements.marketCount.textContent = `${state.markets.length} markets`;

    // Update WS subscription to match
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        const wantTickers = new Set(state.markets.map(m => m.market_ticker));
        const subscribeTo = [...wantTickers].filter(t => !state.subscribed.has(t));
        const unsubscribeFrom = [...state.subscribed].filter(t => !wantTickers.has(t));
        if (subscribeTo.length) {
            state.ws.send(JSON.stringify({ type: 'subscribe', tickers: subscribeTo }));
        }
        if (unsubscribeFrom.length) {
            state.ws.send(JSON.stringify({ type: 'unsubscribe', tickers: unsubscribeFrom }));
        }
        state.subscribed = wantTickers;
    }
}

function groupByEvent() {
    state.eventGroups = {};
    for (const m of state.markets) {
        const ev = m.event_ticker;
        if (!state.eventGroups[ev]) state.eventGroups[ev] = [];
        state.eventGroups[ev].push(m);
    }
}

// ----- Rendering -----
function renderMarkets() {
    const container = elements.container;
    container.innerHTML = '';
    if (state.markets.length === 0) {
        container.innerHTML = `<div class="empty-state">No active markets match the current filter.</div>`;
        return;
    }

    const now = Date.now() / 1000;
    for (const [ev, markets] of Object.entries(state.eventGroups)) {
        container.appendChild(renderEventCard(ev, markets, now));
    }
}

function renderEventCard(ev, markets, now) {
    const card = document.createElement('div');
    card.className = 'market-card';

    const m0 = markets[0];
    const league = m0.league || 'Other';
    const eventTitle = m0.event_title || m0.market_title || ev;
    const closeTs = Math.min(...markets.map(m => m.close_time_unix || Infinity));
    const seconds = closeTs !== Infinity ? (closeTs - now) : null;
    let closeStr = '—';
    if (seconds !== null) {
        if (seconds < 0)         closeStr = 'in progress';
        else if (seconds < 60)   closeStr = '<1min';
        else if (seconds < 3600) closeStr = `${Math.round(seconds / 60)}min`;
        else if (seconds < 86400) closeStr = `${(seconds / 3600).toFixed(1)}h`;
        else                     closeStr = `${Math.round(seconds / 86400)}d`;
    }

    const header = document.createElement('div');
    header.className = 'market-header';
    header.innerHTML = `
        <span class="league-badge">${league}</span>
        <span class="market-title">${escapeHtml(eventTitle)}</span>
        <span class="close-time">⏱ ${closeStr}</span>
    `;
    card.appendChild(header);

    // Look for a live game where either team matches one of this event's market tickers
    const teamCodes = markets.map(m => m.market_ticker.split('-').pop());
    const game = teamCodes.map(t => state.gameByTeam[t]).find(g => g);
    if (game) {
        card.appendChild(renderGameStrip(game));
    }

    for (const m of markets) {
        card.appendChild(renderMarketSide(m));
    }
    return card;
}

function renderGameStrip(g) {
    const strip = document.createElement('div');
    strip.className = 'game-strip';
    strip.dataset.gameId = g.game_id;

    // Game clock: latest_frame_ts - game_start_ts
    let clock = '—';
    if (g.game_start_ts_unix && g.frame_ts_unix) {
        const sec = Math.max(0, g.frame_ts_unix - g.game_start_ts_unix);
        const m = Math.floor(sec / 60), s = sec % 60;
        clock = `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    }
    const stateLbl = g.game_state || '—';
    const bg = g.blue_gold ?? 0, rg = g.red_gold ?? 0;
    const goldDiff = g.gold_diff;
    const diffStr = (goldDiff != null)
        ? (goldDiff > 0 ? `🔵 +${(goldDiff/1000).toFixed(1)}k` : `🔴 +${(-goldDiff/1000).toFixed(1)}k`)
        : '—';
    const winner = g.winner_side
        ? `<span class="game-winner">✓ ${g.winner_side.toUpperCase()} won</span>`
        : '';

    strip.innerHTML = `
        <span class="game-state ${stateLbl === 'in_game' ? 'live' : ''}">${stateLbl}</span>
        <span class="game-clock">${clock}</span>
        <span class="game-teams">
            <span class="blue">${escapeHtml(g.blue_team_code || '?')} ${g.blue_kills ?? 0}/${g.blue_towers ?? 0}T/${g.blue_dragons ?? 0}D/${g.blue_barons ?? 0}B</span>
            <span class="vs">vs</span>
            <span class="red">${escapeHtml(g.red_team_code || '?')} ${g.red_kills ?? 0}/${g.red_towers ?? 0}T/${g.red_dragons ?? 0}D/${g.red_barons ?? 0}B</span>
        </span>
        <span class="game-gold">${diffStr} · 🔵${(bg/1000).toFixed(1)}k 🔴${(rg/1000).toFixed(1)}k</span>
        ${winner}
    `;
    return strip;
}

function renderMarketSide(m) {
    const side = document.createElement('div');
    side.className = 'market-side';
    side.dataset.ticker = m.market_ticker;

    const team = m.market_ticker.split('-').pop();
    const bid = m.yes_bid_cents || 0;
    const ask = m.yes_ask_cents || 0;
    const noPrice = bid > 0 ? 100 - bid : 0;

    const pred = state.predictions[m.market_ticker];
    let modelStr = '<span class="model-na">—</span>';
    let edgeYesCls = '', edgeNoCls = '';
    if (pred) {
        const modelPct = Math.round(pred.yes_prob * 100);
        const lo = Math.round(pred.p10 * 100);
        const hi = Math.round(pred.p90 * 100);
        modelStr = `<span class="model-p" title="band ${lo}-${hi}¢">${modelPct}¢ <span class="model-band">[${lo}-${hi}]</span></span>`;
        if (pred.edge_buy_yes != null && pred.edge_buy_yes > 0.05) edgeYesCls = ' edge-pos';
        if (pred.edge_buy_yes != null && pred.edge_buy_yes < -0.05) edgeYesCls = ' edge-neg';
        if (pred.edge_buy_no != null && pred.edge_buy_no > 0.05) edgeNoCls = ' edge-pos';
        if (pred.edge_buy_no != null && pred.edge_buy_no < -0.05) edgeNoCls = ' edge-neg';
    }

    side.innerHTML = `
        <span class="team-label">${escapeHtml(team)}</span>
        <span class="bid-ask"><span class="bid bid-val">${bid}¢</span> / <span class="ask ask-val">${ask}¢</span></span>
        <span class="model-cell">${modelStr}</span>
        <button class="book-toggle">📊 book</button>
        <button class="btn-buy-yes${edgeYesCls}" data-ticker="${m.market_ticker}" data-side="YES" data-price="${ask}" ${ask <= 0 ? 'disabled' : ''}>BUY YES @ ${ask}¢</button>
        <button class="btn-buy-no${edgeNoCls}" data-ticker="${m.market_ticker}" data-side="NO" data-price="${noPrice}" ${noPrice <= 0 || noPrice >= 100 ? 'disabled' : ''}>BUY NO @ ${noPrice}¢</button>
    `;

    // Orderbook expander (separate row below)
    const bookContainer = document.createElement('div');
    bookContainer.className = 'orderbook';
    bookContainer.dataset.bookTicker = m.market_ticker;
    side.appendChild(bookContainer);

    // Wire up
    side.querySelector('.book-toggle').addEventListener('click', () => toggleBook(m.market_ticker, bookContainer));
    side.querySelector('.btn-buy-yes').addEventListener('click', (e) => onBuyClick(e, m));
    side.querySelector('.btn-buy-no').addEventListener('click', (e) => onBuyClick(e, m));

    return side;
}

async function toggleBook(ticker, el) {
    if (state.expandedBooks.has(ticker)) {
        el.classList.remove('open');
        state.expandedBooks.delete(ticker);
        return;
    }
    state.expandedBooks.add(ticker);
    el.classList.add('open');
    // Fetch initial state
    try {
        const r = await fetch(`/api/orderbook/${encodeURIComponent(ticker)}`);
        if (r.ok) {
            const data = await r.json();
            renderBook(el, { bids: data.bids, asks: data.asks, ts: data.updated_at * 1000 });
        } else {
            el.innerHTML = `<div class="book-meta">No orderbook data yet — waiting for stream…</div>`;
        }
    } catch (e) {
        el.innerHTML = `<div class="book-meta">Error: ${e}</div>`;
    }
}

async function onBuyClick(e, market) {
    const btn = e.currentTarget;
    const side = btn.dataset.side;
    const price = parseInt(btn.dataset.price);
    const ticker = btn.dataset.ticker;
    const contracts = parseInt(elements.defaultContracts.value);
    const padding = parseInt(elements.limitPadding.value) || 0;
    const limitPrice = side === 'YES' ? price + padding : price + padding;

    btn.disabled = true;
    btn.textContent = '…';
    try {
        const r = await fetch('/api/trade', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                market_ticker: ticker,
                side,
                contracts,
                limit_price_cents: Math.min(99, Math.max(1, limitPrice)),
                live_mode: state.liveMode,
            }),
        });
        const data = await r.json();
        if (!r.ok) {
            toast(`Trade failed: ${data.detail || r.statusText}`, 'error', 6000);
        } else {
            const emoji = data.mode === 'live' ? '🔴' : '📝';
            toast(`${emoji} ${data.mode.toUpperCase()}: ${side} × ${contracts} @ ${data.fill_price_cents}¢ — #${data.trade_id}`, 'success', 5000);
            fetchTrades();
        }
    } catch (err) {
        toast(`Trade error: ${err.message}`, 'error', 6000);
    } finally {
        btn.disabled = false;
    }
}

function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

// ----- Filter + setting controls -----
elements.leagueFilter.addEventListener('change', () => {
    state.league = elements.leagueFilter.value;
    fetchMarkets();
});

elements.defaultContracts.addEventListener('change', () => {
    state.defaultContracts = parseInt(elements.defaultContracts.value);
});

elements.limitPadding.addEventListener('change', () => {
    state.limitPadding = parseInt(elements.limitPadding.value);
});

elements.refreshBtn.addEventListener('click', async () => {
    elements.refreshBtn.disabled = true;
    elements.refreshBtn.textContent = 'Refreshing…';
    try {
        const r = await fetch('/api/refresh-markets', { method: 'POST' });
        if (r.ok) {
            const d = await r.json();
            toast(`Refreshed ${d.count} markets`, 'success');
            await fetchMarkets();
        } else {
            toast('Refresh failed', 'error');
        }
    } catch (e) {
        toast(`Refresh error: ${e.message}`, 'error');
    } finally {
        elements.refreshBtn.disabled = false;
        elements.refreshBtn.textContent = '🔄 Refresh markets';
    }
});

elements.liveToggle.addEventListener('change', () => {
    state.liveMode = elements.liveToggle.checked;
    if (state.liveMode) {
        elements.modeLabel.textContent = '🔴 LIVE — real money';
        elements.modeLabel.className = 'mode-live';
    } else {
        elements.modeLabel.textContent = '📝 PAPER — no money';
        elements.modeLabel.className = 'mode-paper';
    }
});

// ----- Keep-alive ping every 25s -----
setInterval(() => {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({ type: 'ping' }));
    }
}, 25000);

// ----- Periodic game state refresh -----
async function refreshGames() {
    await fetchGames();
    // Update strips in-place; if a market has a game now and didn't before
    // (or vice versa), re-render so the strip appears/disappears.
    let needsRerender = false;
    for (const card of document.querySelectorAll('.market-card')) {
        const teamCodes = [...card.querySelectorAll('.team-label')].map(e => e.textContent);
        const game = teamCodes.map(t => state.gameByTeam[t]).find(g => g);
        const existingStrip = card.querySelector('.game-strip');
        if (game && !existingStrip) { needsRerender = true; break; }
        if (!game && existingStrip) { needsRerender = true; break; }
        if (game && existingStrip) {
            // Replace strip content with fresh data
            const fresh = renderGameStrip(game);
            existingStrip.replaceWith(fresh);
        }
    }
    if (needsRerender) renderMarkets();
}
// 10s fallback refresh — WS push handles per-frame updates. This catches new
// games appearing or pollers reconnecting after errors.
setInterval(refreshGames, 10000);

// ----- Periodic trades + P&L refresh (mark-to-market) -----
setInterval(fetchTrades, 2000);

// ----- Periodic predictions refresh (pre-match features change rarely) -----
setInterval(fetchPredictions, 60000);

// ----- Boot -----
(async () => {
    await fetchPredictions();
    await fetchGames();
    await fetchMarkets();
    await fetchTrades();
    connectWS();
})();
