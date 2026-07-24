/*
 * Game detail — one page for both live and finished games.
 *
 * Move-by-move stepping is entirely client-side (nav buttons, ← → / Home / End
 * keys, clicking a move or a history entry), driven by the data the server
 * serialises into #gr-data (plies, positions, per-move coach analysis, and — for
 * a live game — the current to-move position).
 *
 * A live game additionally polls the server for new moves and merges them in:
 * if you are sitting at the live head it keeps following the game, otherwise it
 * leaves you where you are and offers a "jump to live" button. The coach panel at
 * the live head shows the suggestion for the position you're about to play; step
 * back and it reviews any past move. The only writes are the on-demand
 * "Request suggestion" POST and its status poll.
 */
(function () {
  "use strict";

  var dataEl = document.getElementById("gr-data");
  if (!dataEl) return;

  var DATA = JSON.parse(dataEl.textContent);

  var GLYPH = { k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟" };

  var meta = function () { return DATA.meta; };
  var plies = function () { return DATA.plies; };
  var positions = function () { return DATA.positions; };
  var analysis = function () { return DATA.analysis; };
  var flipped = function () { return meta().orientation === "black"; };
  var canAnalyze = function () { return !!meta().canAnalyze; };

  // Start at the live head for a live game, at the opening for a finished one.
  var sel = meta().isLive ? meta().liveHead : 0;

  var el = function (id) { return document.getElementById(id); };
  var boardEl = el("gr-board");
  var arrowsEl = el("gr-arrows");
  var evalFillEl = el("gr-evalfill");
  var coachEl = el("gr-coach");
  var historyEl = el("gr-history");
  var hcountEl = el("gr-hcount");
  var movesEl = el("gr-moves");
  var lastMoveEl = el("gr-lastmove");
  var selTextEl = el("gr-seltext");
  var moveLabelEl = el("gr-movelabel");
  var jumpLiveEl = el("gr-jumplive");
  var jumpLiveTextEl = el("gr-jumplive-text");
  var pillEl = el("gr-pill");
  var pillTextEl = el("gr-pill-text");

  if (!boardEl) return;

  function esc(s) {
    return (s == null ? "" : String(s)).replace(/[&<>"]/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch];
    });
  }

  function an(i) { return analysis()[String(i)] || null; }
  function atLiveHead() { return meta().isLive && sel === meta().liveHead; }

  // ---- board -------------------------------------------------------------
  function fenToCells(fen, hlSet) {
    var boardPart = (fen || "").split(" ")[0];
    var rows = boardPart.split("/");
    var cells = [];
    for (var r = 0; r < 8; r++) {
      var rank = 8 - r;
      var parsed = [];
      var row = rows[r] || "8";
      for (var k = 0; k < row.length; k++) {
        var ch = row[k];
        if (ch >= "1" && ch <= "8") { for (var n = 0; n < +ch; n++) parsed.push(""); }
        else parsed.push(ch);
      }
      while (parsed.length < 8) parsed.push("");
      for (var c = 0; c < 8; c++) {
        var piece = parsed[c];
        var name = String.fromCharCode(97 + c) + rank;
        cells.push({
          glyph: piece ? GLYPH[piece.toLowerCase()] : "",
          light: (r + c) % 2 === 0,
          white: piece ? piece === piece.toUpperCase() : false,
          highlight: hlSet.has(name),
        });
      }
    }
    if (flipped()) cells.reverse();
    return cells;
  }

  function renderBoard(fen, hlSet) {
    var cells = fenToCells(fen, hlSet);
    var html = "";
    for (var i = 0; i < cells.length; i++) {
      var c = cells[i];
      html +=
        '<div class="board__sq ' +
        (c.light ? "board__sq--light" : "board__sq--dark") +
        (c.highlight ? " board__sq--hl" : "") + '">' +
        (c.glyph
          ? '<span class="board__pc ' + (c.white ? "board__pc--w" : "board__pc--b") +
            '">' + c.glyph + "</span>"
          : "") + "</div>";
    }
    boardEl.innerHTML = html;
  }

  function center(sq) {
    var c = sq.charCodeAt(0) - 97;
    var rowFromTop = 8 - +sq[1];
    if (flipped()) { c = 7 - c; rowFromTop = 7 - rowFromTop; }
    return {
      x: (((c + 0.5) / 8) * 100).toFixed(2),
      y: (((rowFromTop + 0.5) / 8) * 100).toFixed(2),
    };
  }

  function renderArrows(list) {
    var defs =
      "<defs>" +
      '<marker id="gr-ah-brass" markerWidth="4" markerHeight="4" refX="2.4" refY="2" orient="auto"><path d="M0,0 L4,2 L0,4 z" fill="#b78e54"/></marker>' +
      '<marker id="gr-ah-green" markerWidth="4" markerHeight="4" refX="2.4" refY="2" orient="auto"><path d="M0,0 L4,2 L0,4 z" fill="#4a7a52"/></marker>' +
      "</defs>";
    var lines = "";
    for (var i = 0; i < list.length; i++) {
      var a = list[i];
      lines +=
        '<line x1="' + a.x1 + '%" y1="' + a.y1 + '%" x2="' + a.x2 + '%" y2="' + a.y2 +
        '%" stroke="' + a.color + '" stroke-width="7" stroke-linecap="round" opacity="' +
        a.opacity + '" marker-end="' + a.marker + '"/>';
    }
    arrowsEl.innerHTML = defs + lines;
  }

  function evalFillFor(upTo) {
    var fill = 50;
    for (var i = 1; i <= upTo; i++) {
      var a = an(i);
      if (a && typeof a.fill === "number") fill = a.fill;
    }
    if (atLiveHead()) {
      var ha = meta().headAnalysis;
      if (ha && typeof ha.fill === "number") fill = ha.fill;
    }
    return fill;
  }

  // ---- coach panel states ------------------------------------------------
  function moveRef(ply) {
    return ply.no + (ply.color === "white" ? ". " : "… ") + ply.san;
  }

  function startState() {
    return (
      '<div class="coach__placeholder"><span class="gr-glyph">♚</span>' +
      "<p>Step through the moves to review the position and the coach's suggestion for each one.</p></div>"
    );
  }
  function opponentState(ply) {
    return (
      '<div class="gr-opp"><div class="coach__rec-label">Opponent’s move</div>' +
      '<div class="gr-bigmove">' + esc(ply.san) + "</div>" +
      '<p class="coach__summary">The coach only analyses your moves. Step to your turn to see the suggestion.</p></div>'
    );
  }
  function pendingState(what) {
    return (
      '<div class="coach__placeholder"><span class="spinner"></span>' +
      "<p>Analysing " + what + " in the background… the suggestion will appear shortly.</p></div>"
    );
  }
  function unanalyzedState(ply) {
    return (
      '<div class="coach__placeholder"><span class="gr-glyph gr-glyph--sm">♞</span>' +
      "<p>No suggestion requested for <b>" + esc(ply.san) + "</b>." +
      (canAnalyze() ? " You can ask the coach for one." : "") + "</p>" +
      (canAnalyze()
        ? '<button type="button" class="btn btn--accent btn--sm" data-request-ply>' +
          '<i class="fa-solid fa-wand-magic-sparkles"></i> Request suggestion</button>'
        : "") + "</div>"
    );
  }
  function analyzedState(ply, a) {
    var followed = a.followed;
    var statusText = followed ? "You played the best move" : "The coach preferred " + esc(a.recSan);
    return (
      "<div>" +
      '<div class="coach__rec"><div><div class="coach__rec-label">Recommended</div>' +
      '<div class="coach__move">' + esc(a.recSan) + "</div></div>" +
      (a.recEval ? '<div style="padding-bottom:6px"><div class="coach__eval">' + esc(a.recEval) + "</div></div>" : "") +
      '<span class="coach__badge">BEST MOVE</span></div>' +
      '<div class="gr-compare"><div class="gr-compare__cell">' +
      '<div class="gr-compare__label">You played</div>' +
      '<div class="gr-compare__val"><span class="gr-compare__san">' + esc(ply.san) + "</span>" +
      '<span class="gr-compare__eval">' + esc(a.playedEval || "—") + "</span></div></div>" +
      '<div class="gr-compare__cell gr-compare__cell--' + (followed ? "good" : "diff") + '">' +
      '<div class="gr-compare__label">Coach</div>' +
      '<div class="gr-compare__val"><span class="gr-compare__san">' + esc(a.recSan) + "</span>" +
      '<span class="gr-compare__eval gr-compare__eval--green">' + esc(a.recEval) + "</span></div></div></div>" +
      '<div class="gr-status gr-status--' + (followed ? "followed" : "differed") + '"><span>' +
      (followed ? "✓" : "◆") + "</span>" + statusText + "</div>" +
      (a.prose ? '<p class="coach__summary">' + esc(a.prose) + "</p>" : "") +
      '<div class="gr-legend"><span class="gr-legend__item"><span class="gr-legend__dot gr-legend__dot--brass"></span>Recommended</span>' +
      (followed ? "" : '<span class="gr-legend__item"><span class="gr-legend__dot gr-legend__dot--green"></span>Played</span>') +
      "</div></div>"
    );
  }

  // Live to-move states (at the head of a live game).
  function liveWaitingState() {
    return (
      '<div class="coach__placeholder"><i class="fa-solid fa-hourglass-half gr-glyph gr-glyph--sm"></i>' +
      "<p>Opponent to move. The coach will suggest your move once it's your turn.</p></div>"
    );
  }
  function liveRequestState() {
    return (
      '<div class="coach__placeholder"><span class="gr-glyph gr-glyph--sm">♞</span>' +
      "<p>It's your move." + (canAnalyze() ? " Ask the coach what to play." : "") + "</p>" +
      (canAnalyze()
        ? '<button type="button" class="btn btn--accent btn--block" data-request-head>' +
          '<i class="fa-solid fa-wand-magic-sparkles"></i> Request suggestion</button>'
        : "") + "</div>"
    );
  }
  function liveAnalyzedState(a) {
    return (
      "<div>" +
      '<div class="coach__rec"><div><div class="coach__rec-label">Recommended</div>' +
      '<div class="coach__move">' + esc(a.recSan) + "</div></div>" +
      (a.recEval ? '<div style="padding-bottom:6px"><div class="coach__eval">' + esc(a.recEval) + "</div></div>" : "") +
      '<span class="coach__badge">BEST MOVE</span></div>' +
      '<div class="gr-status gr-status--followed" style="margin-top:14px"><span>♚</span>It\'s your move — this is the coach\'s pick</div>' +
      (a.prose ? '<p class="coach__summary">' + esc(a.prose) + "</p>" : "") +
      '<div class="gr-legend"><span class="gr-legend__item"><span class="gr-legend__dot gr-legend__dot--brass"></span>Recommended</span></div>' +
      "</div>"
    );
  }

  function renderCoach() {
    // Live head: the position you're about to play (or waiting on the opponent).
    if (atLiveHead()) {
      moveLabelEl.textContent = "Live position";
      if (!meta().userToMove) { coachEl.innerHTML = liveWaitingState(); return; }
      var ha = meta().headAnalysis;
      if (!ha) { coachEl.innerHTML = liveRequestState(); wireHeadRequest(); return; }
      if (ha.pending) { coachEl.innerHTML = pendingState("the current position"); return; }
      coachEl.innerHTML = liveAnalyzedState(ha);
      return;
    }

    var ply = sel > 0 ? plies()[sel - 1] : null;
    moveLabelEl.textContent = ply ? "Move " + ply.no + " · " + (ply.color === "white" ? "White" : "Black") : "";
    if (!ply) { coachEl.innerHTML = startState(); return; }
    if (ply.color === "black") { coachEl.innerHTML = opponentState(ply); return; }

    var a = an(sel);
    if (a && a.pending) { coachEl.innerHTML = pendingState("<b>" + esc(ply.san) + "</b>"); return; }
    if (!a) { coachEl.innerHTML = unanalyzedState(ply); wirePlyRequest(sel, ply); return; }
    coachEl.innerHTML = analyzedState(ply, a);
  }

  // ---- moves grid + history ---------------------------------------------
  function renderMoves() {
    var html = "";
    var ps = plies();
    for (var i = 0; i < ps.length; i++) {
      var p = ps[i];
      var idx = i + 1;
      var a = p.color === "white" ? an(idx) : null;
      var done = a && !a.pending;
      var selected = idx === sel;
      var cls = "gr-move";
      if (done) cls += " gr-move--analyzed";
      if (selected) cls += " gr-move--sel";
      var badge = "";
      if (done) {
        badge =
          '<span class="gr-badge ' + (a.followed ? "gr-badge--followed" : "gr-badge--differed") + '" title="' +
          (a.followed ? "You played the coach’s move" : "Coach suggested " + esc(a.recSan)) + '">' +
          (a.followed ? "✓" : "◆") + " " + esc(a.recSan) + "</span>";
      } else if (a && a.pending) {
        badge = '<span class="gr-badge gr-badge--pending" title="Analysing…">…</span>';
      }
      html +=
        '<li class="' + cls + '" data-sel="' + idx + '">' +
        '<span class="gr-move__no">' + p.no + (p.color === "white" ? "." : "…") + "</span>" +
        '<span class="gr-move__san">' + esc(p.san) + "</span>" + badge + "</li>";
    }
    movesEl.innerHTML = html;
  }

  function renderHistory() {
    var a = analysis();
    var idxs = [];
    for (var key in a) {
      if (!Object.prototype.hasOwnProperty.call(a, key)) continue;
      if (a[key] && !a[key].pending) idxs.push(+key);
    }
    idxs.sort(function (x, y) { return x - y; });
    hcountEl.textContent = idxs.length;
    var html = "";
    for (var i = 0; i < idxs.length; i++) {
      var idx = idxs[i];
      var p = plies()[idx - 1];
      var a2 = a[String(idx)];
      if (!p) continue;
      var followed = a2.followed;
      html +=
        '<li class="gr-hist__item' + (idx === sel ? " gr-hist__item--sel" : "") + '" data-sel="' + idx + '">' +
        '<div class="gr-hist__top"><span class="gr-hist__no">Move ' + p.no + "</span>" +
        '<span class="gr-hist__san">' + esc(a2.recSan) + "</span>" +
        '<span class="gr-tag ' + (followed ? "gr-tag--followed" : "gr-tag--differed") + '">' +
        (followed ? "followed" : "differed") + "</span></div>" +
        (a2.recEval ? '<div class="gr-hist__eval">' + esc(a2.recEval) + "</div>" : "") +
        (a2.prose ? '<p class="gr-hist__prose">' + esc(a2.prose) + "</p>" : "") + "</li>";
    }
    historyEl.innerHTML = html;
  }

  // ---- main render -------------------------------------------------------
  function render() {
    var ply = sel > 0 ? plies()[sel - 1] : null;

    var hlSet = new Set();
    if (ply) { if (ply.from) hlSet.add(ply.from); if (ply.to) hlSet.add(ply.to); }
    renderBoard(positions()[sel] || positions()[0] || "", hlSet);

    var arrows = [];
    if (atLiveHead()) {
      var ha = meta().headAnalysis;
      if (ha && !ha.pending && ha.recFrom && ha.recTo) {
        var hf = center(ha.recFrom), ht = center(ha.recTo);
        arrows.push({ x1: hf.x, y1: hf.y, x2: ht.x, y2: ht.y, color: "#b78e54", opacity: 0.95, marker: "url(#gr-ah-brass)" });
      }
    } else {
      var a = ply && ply.color === "white" ? an(sel) : null;
      if (a && !a.pending) {
        if (!a.followed && ply.from && ply.to) {
          var pf = center(ply.from), pt = center(ply.to);
          arrows.push({ x1: pf.x, y1: pf.y, x2: pt.x, y2: pt.y, color: "#4a7a52", opacity: 0.85, marker: "url(#gr-ah-green)" });
        }
        var rf0 = a.recFrom || ply.from, rt0 = a.recTo || ply.to;
        if (rf0 && rt0) {
          var rf = center(rf0), rt = center(rt0);
          arrows.push({ x1: rf.x, y1: rf.y, x2: rt.x, y2: rt.y, color: "#b78e54", opacity: 0.95, marker: "url(#gr-ah-brass)" });
        }
      }
    }
    renderArrows(arrows);

    evalFillEl.style.height = evalFillFor(sel) + "%";
    lastMoveEl.textContent = "Last: " + (ply ? moveRef(ply) : "—");
    selTextEl.textContent = atLiveHead()
      ? (meta().userToMove ? "Live · your move" : "Live · opponent to move")
      : (ply ? "Reviewing: " + moveRef(ply) : "Starting position");

    renderCoach();
    renderMoves();
    renderHistory();
    updateJumpLive();
  }

  function select(i) {
    sel = Math.max(0, Math.min(plies().length, i));
    render();
  }
  function step(d) { select(sel + d); }

  function updateJumpLive() {
    if (!jumpLiveEl) return;
    var behind = meta().isLive && sel < meta().liveHead;
    jumpLiveEl.hidden = !behind;
  }

  // ---- on-demand analysis ------------------------------------------------
  function postAnalyze(fen) {
    return fetch(DATA.urls.analyze, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded", "X-CSRFToken": DATA.csrf },
      body: "fen=" + encodeURIComponent(fen),
    });
  }

  function wirePlyRequest(idx, ply) {
    var btn = coachEl.querySelector("[data-request-ply]");
    if (btn) btn.addEventListener("click", function () { requestPly(idx, ply); });
  }
  function wireHeadRequest() {
    var btn = coachEl.querySelector("[data-request-head]");
    if (btn) btn.addEventListener("click", requestHead);
  }

  function requestPly(idx, ply) {
    if (!canAnalyze() || !ply.fenBefore) return;
    analysis()[String(idx)] = { pending: true };
    if (sel === idx) render(); else renderMoves();
    postAnalyze(ply.fenBefore).then(function () { pollPly(idx, ply); }).catch(function () {});
  }
  function requestHead() {
    if (!canAnalyze() || !meta().headFen) return;
    meta().headAnalysis = { pending: true };
    render();
    // The live poll loop picks up the DONE result; kick one poll immediately too.
    postAnalyze(meta().headFen).then(function () { pollOnce(); }).catch(function () {});
  }

  function applyPly(idx, ply, j) {
    if (j.status !== "done") return false;
    var followed = j.recSan === ply.san;
    analysis()[String(idx)] = {
      recSan: j.recSan, recEval: j.recEval, playedEval: followed ? j.recEval : "",
      fill: j.fill, followed: followed, prose: j.prose, recFrom: j.recFrom, recTo: j.recTo,
    };
    if (sel === idx) render(); else { renderMoves(); renderHistory(); }
    return true;
  }
  function pollPly(idx, ply) {
    var tries = 0;
    var tick = function () {
      tries++;
      fetch(DATA.urls.analyze + "?fen=" + encodeURIComponent(ply.fenBefore))
        .then(function (r) { return r.json(); })
        .then(function (j) { if (!applyPly(idx, ply, j) && tries < 60) setTimeout(tick, 2000); })
        .catch(function () { if (tries < 60) setTimeout(tick, 3000); });
    };
    setTimeout(tick, 2000);
  }

  // ---- live polling + merge ---------------------------------------------
  function mergeData(fresh) {
    var wasFollowing = sel === DATA.meta.liveHead;

    // Keep any optimistic pending the user set that the server hasn't caught yet.
    var mergedAnalysis = fresh.analysis || {};
    var old = DATA.analysis || {};
    for (var k in old) {
      if (old[k] && old[k].pending && !(k in mergedAnalysis)) mergedAnalysis[k] = old[k];
    }
    DATA.plies = fresh.plies;
    DATA.positions = fresh.positions;
    DATA.analysis = mergedAnalysis;
    DATA.meta = fresh.meta;

    if (wasFollowing) sel = fresh.meta.liveHead;
    else sel = Math.max(0, Math.min(fresh.plies.length, sel));

    if (!fresh.meta.isLive) {
      stopPolling();
      if (pillEl) pillEl.classList.remove("status-pill--watch");
      if (pillTextEl) pillTextEl.textContent = "REVIEW";
    }
    if (jumpLiveTextEl) {
      var behind = fresh.meta.liveHead - sel;
      jumpLiveTextEl.textContent = behind > 0 ? behind + " new · live" : "Live";
    }
    render();
  }

  var pollTimer = null;
  function pollOnce() {
    return fetch(DATA.urls.poll, { headers: { "X-Requested-With": "poll" } })
      .then(function (r) { return r.status === 204 ? null : r.json(); })
      .then(function (j) { if (j) mergeData(j); })
      .catch(function () {});
  }
  function startPolling() {
    if (pollTimer || !DATA.meta.isLive) return;
    pollTimer = setInterval(pollOnce, 5000);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // ---- events ------------------------------------------------------------
  var navButtons = document.querySelectorAll("#gr-root [data-nav]");
  for (var i = 0; i < navButtons.length; i++) {
    navButtons[i].addEventListener("click", function () {
      var k = this.getAttribute("data-nav");
      if (k === "first") select(0);
      else if (k === "prev") step(-1);
      else if (k === "next") step(1);
      else if (k === "last") select(plies().length);
    });
  }
  if (jumpLiveEl) jumpLiveEl.addEventListener("click", function () { select(meta().liveHead); });

  function delegateSelect(e) {
    var li = e.target.closest("[data-sel]");
    if (li) select(+li.getAttribute("data-sel"));
  }
  movesEl.addEventListener("click", delegateSelect);
  historyEl.addEventListener("click", delegateSelect);

  window.addEventListener("keydown", function (e) {
    if (e.key === "ArrowLeft") { step(-1); e.preventDefault(); }
    else if (e.key === "ArrowRight") { step(1); e.preventDefault(); }
    else if (e.key === "Home") { select(0); }
    else if (e.key === "End") { select(plies().length); }
  });

  render();
  startPolling();
})();
