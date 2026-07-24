/*
 * Game Review — move-by-move stepping.
 *
 * Ports the review mockup's client controller to vanilla JS, driven by the real
 * game data the server serialises into #gr-data (plies, positions, per-move coach
 * analysis). Stepping stays entirely client-side — nav buttons, ← → / Home / End
 * keys and clicking a move/history entry all just re-render from the embedded
 * data, so there is no round trip per move. The only server calls are the
 * on-demand "Request suggestion" POST and its status poll.
 */
(function () {
  "use strict";

  var dataEl = document.getElementById("gr-data");
  if (!dataEl) return;

  var DATA = JSON.parse(dataEl.textContent);
  var plies = DATA.plies || [];
  var positions = DATA.positions || [];
  var analysis = DATA.analysis || {};
  var meta = DATA.meta || {};
  var flipped = meta.orientation === "black";
  var canAnalyze = !!meta.canAnalyze;

  // Black-piece glyphs, keyed by lowercase letter — colour is conveyed by CSS on
  // the piece span (same convention as the server-side board renderer).
  var GLYPH = { k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟" };

  var sel = 0;

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

  if (!boardEl) return; // page rendered its empty state — nothing to drive

  function esc(s) {
    return (s == null ? "" : String(s)).replace(/[&<>"]/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch];
    });
  }

  function an(i) {
    var a = analysis[String(i)];
    return a || null;
  }

  // ---- board -------------------------------------------------------------
  // Expand a FEN into 64 cells (rank 8→1, file a→h), reversed when the board is
  // flipped to the black perspective — mirrors services/board.fen_to_cells.
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
        if (ch >= "1" && ch <= "8") {
          for (var n = 0; n < +ch; n++) parsed.push("");
        } else {
          parsed.push(ch);
        }
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
    if (flipped) cells.reverse();
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
        (c.highlight ? " board__sq--hl" : "") +
        '">' +
        (c.glyph
          ? '<span class="board__pc ' +
            (c.white ? "board__pc--w" : "board__pc--b") +
            '">' + c.glyph + "</span>"
          : "") +
        "</div>";
    }
    boardEl.innerHTML = html;
  }

  // Square centre as an SVG percentage coordinate, respecting board flip.
  function center(sq) {
    var c = sq.charCodeAt(0) - 97; // file 0..7
    var rowFromTop = 8 - +sq[1]; // rank 8 at top
    if (flipped) { c = 7 - c; rowFromTop = 7 - rowFromTop; }
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

  // Eval fill carries the last analysed value forward across un-analysed plies.
  function evalFillFor(upTo) {
    var fill = 50;
    for (var i = 1; i <= upTo; i++) {
      var a = an(i);
      if (a && typeof a.fill === "number") fill = a.fill;
    }
    return fill;
  }

  // ---- coach panel states ------------------------------------------------
  function moveRef(ply) {
    return ply.no + (ply.color === "white" ? ". " : "… ") + ply.san;
  }

  function startState() {
    return (
      '<div class="coach__placeholder">' +
      '<span class="gr-glyph">♚</span>' +
      "<p>Step through the moves to review the position and the coach's suggestion for each one.</p>" +
      "</div>"
    );
  }

  function opponentState(ply) {
    return (
      '<div class="gr-opp">' +
      '<div class="coach__rec-label">Opponent’s move</div>' +
      '<div class="gr-bigmove">' + esc(ply.san) + "</div>" +
      '<p class="coach__summary">The coach only analyses your moves. Step forward to your turn to see the suggestion.</p>' +
      "</div>"
    );
  }

  function pendingState(ply) {
    return (
      '<div class="coach__placeholder">' +
      '<span class="spinner"></span>' +
      "<p>Analysing <b>" + esc(ply.san) + "</b> in the background… the suggestion will appear shortly.</p>" +
      "</div>"
    );
  }

  function unanalyzedState(ply) {
    return (
      '<div class="coach__placeholder">' +
      '<span class="gr-glyph gr-glyph--sm">♞</span>' +
      "<p>No suggestion requested for <b>" + esc(ply.san) + "</b>." +
      (canAnalyze ? " You can ask the coach for one." : "") + "</p>" +
      (canAnalyze
        ? '<button type="button" class="btn btn--accent btn--sm" data-request>' +
          '<i class="fa-solid fa-wand-magic-sparkles"></i> Request suggestion</button>'
        : "") +
      "</div>"
    );
  }

  function analyzedState(ply, a) {
    var followed = a.followed;
    var statusText = followed ? "You played the best move" : "The coach preferred " + esc(a.recSan);
    var statusIcon = followed ? "✓" : "◆";
    return (
      "<div>" +
      '<div class="coach__rec">' +
      '<div><div class="coach__rec-label">Recommended</div><div class="coach__move">' + esc(a.recSan) + "</div></div>" +
      (a.recEval ? '<div style="padding-bottom:6px"><div class="coach__eval">' + esc(a.recEval) + "</div></div>" : "") +
      '<span class="coach__badge">BEST MOVE</span>' +
      "</div>" +
      '<div class="gr-compare">' +
      '<div class="gr-compare__cell">' +
      '<div class="gr-compare__label">You played</div>' +
      '<div class="gr-compare__val"><span class="gr-compare__san">' + esc(ply.san) + "</span>" +
      '<span class="gr-compare__eval">' + esc(a.playedEval || "—") + "</span></div>" +
      "</div>" +
      '<div class="gr-compare__cell gr-compare__cell--' + (followed ? "good" : "diff") + '">' +
      '<div class="gr-compare__label">Coach</div>' +
      '<div class="gr-compare__val"><span class="gr-compare__san">' + esc(a.recSan) + "</span>" +
      '<span class="gr-compare__eval gr-compare__eval--green">' + esc(a.recEval) + "</span></div>" +
      "</div>" +
      "</div>" +
      '<div class="gr-status gr-status--' + (followed ? "followed" : "differed") + '"><span>' +
      statusIcon + "</span>" + statusText + "</div>" +
      (a.prose ? '<p class="coach__summary">' + esc(a.prose) + "</p>" : "") +
      '<div class="gr-legend">' +
      '<span class="gr-legend__item"><span class="gr-legend__dot gr-legend__dot--brass"></span>Recommended</span>' +
      (followed ? "" : '<span class="gr-legend__item"><span class="gr-legend__dot gr-legend__dot--green"></span>Played</span>') +
      "</div>" +
      "</div>"
    );
  }

  function renderCoach(cur) {
    var ply = cur > 0 ? plies[cur - 1] : null;
    moveLabelEl.textContent = ply ? "Move " + ply.no + " · " + (ply.color === "white" ? "White" : "Black") : "";

    if (!ply) { coachEl.innerHTML = startState(); return; }
    if (ply.color === "black") { coachEl.innerHTML = opponentState(ply); return; }

    var a = an(cur);
    if (a && a.pending) { coachEl.innerHTML = pendingState(ply); return; }
    if (!a) {
      coachEl.innerHTML = unanalyzedState(ply);
      var btn = coachEl.querySelector("[data-request]");
      if (btn) btn.addEventListener("click", function () { requestSuggestion(cur, ply); });
      return;
    }
    coachEl.innerHTML = analyzedState(ply, a);
  }

  // ---- moves grid + history ---------------------------------------------
  function renderMoves(cur) {
    var html = "";
    for (var i = 0; i < plies.length; i++) {
      var p = plies[i];
      var idx = i + 1;
      var a = p.color === "white" ? an(idx) : null;
      var done = a && !a.pending;
      var selected = idx === cur;
      var cls = "gr-move";
      if (done) cls += " gr-move--analyzed";
      if (selected) cls += " gr-move--sel";
      var badge = "";
      if (done) {
        var followed = a.followed;
        badge =
          '<span class="gr-badge ' + (followed ? "gr-badge--followed" : "gr-badge--differed") + '" title="' +
          (followed ? "You played the coach’s move" : "Coach suggested " + esc(a.recSan)) + '">' +
          (followed ? "✓" : "◆") + " " + esc(a.recSan) + "</span>";
      } else if (a && a.pending) {
        badge = '<span class="gr-badge gr-badge--pending" title="Analysing…">…</span>';
      }
      html +=
        '<li class="' + cls + '" data-sel="' + idx + '">' +
        '<span class="gr-move__no">' + p.no + (p.color === "white" ? "." : "…") + "</span>" +
        '<span class="gr-move__san">' + esc(p.san) + "</span>" +
        badge + "</li>";
    }
    movesEl.innerHTML = html;
  }

  function renderHistory(cur) {
    var idxs = [];
    for (var key in analysis) {
      if (!Object.prototype.hasOwnProperty.call(analysis, key)) continue;
      var a = analysis[key];
      if (a && !a.pending) idxs.push(+key);
    }
    idxs.sort(function (x, y) { return x - y; });
    hcountEl.textContent = idxs.length;

    var html = "";
    for (var i = 0; i < idxs.length; i++) {
      var idx = idxs[i];
      var p = plies[idx - 1];
      var a2 = analysis[String(idx)];
      var followed = a2.followed;
      html +=
        '<li class="gr-hist__item' + (idx === cur ? " gr-hist__item--sel" : "") + '" data-sel="' + idx + '">' +
        '<div class="gr-hist__top">' +
        '<span class="gr-hist__no">Move ' + p.no + "</span>" +
        '<span class="gr-hist__san">' + esc(a2.recSan) + "</span>" +
        '<span class="gr-tag ' + (followed ? "gr-tag--followed" : "gr-tag--differed") + '">' +
        (followed ? "followed" : "differed") + "</span>" +
        "</div>" +
        (a2.recEval ? '<div class="gr-hist__eval">' + esc(a2.recEval) + "</div>" : "") +
        (a2.prose ? '<p class="gr-hist__prose">' + esc(a2.prose) + "</p>" : "") +
        "</li>";
    }
    historyEl.innerHTML = html;
  }

  // ---- main render -------------------------------------------------------
  function render() {
    var ply = sel > 0 ? plies[sel - 1] : null;

    var hlSet = new Set();
    if (ply) { if (ply.from) hlSet.add(ply.from); if (ply.to) hlSet.add(ply.to); }
    renderBoard(positions[sel] || positions[0] || "", hlSet);

    var arrows = [];
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
    renderArrows(arrows);

    evalFillEl.style.height = evalFillFor(sel) + "%";
    lastMoveEl.textContent = "Last: " + (ply ? moveRef(ply) : "—");
    selTextEl.textContent = ply ? "Reviewing: " + moveRef(ply) : "Starting position";

    renderCoach(sel);
    renderMoves(sel);
    renderHistory(sel);
  }

  function select(i) { sel = Math.max(0, Math.min(plies.length, i)); render(); }
  function step(d) { select(sel + d); }

  // ---- on-demand analysis ------------------------------------------------
  function requestSuggestion(idx, ply) {
    if (!canAnalyze || !ply.fenBefore) return;
    analysis[String(idx)] = { pending: true };
    if (sel === idx) render(); else renderMoves(sel);

    fetch(DATA.urls.analyze, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRFToken": DATA.csrf,
      },
      body: "fen=" + encodeURIComponent(ply.fenBefore),
    })
      .then(function (r) { return r.json(); })
      .then(function () { pollStatus(idx, ply); })
      .catch(function () {});
  }

  function applyStatus(idx, ply, j) {
    if (j.status !== "done") return false;
    var followed = j.recSan === ply.san;
    analysis[String(idx)] = {
      recSan: j.recSan,
      recEval: j.recEval,
      playedEval: followed ? j.recEval : "",
      fill: j.fill,
      followed: followed,
      prose: j.prose,
      recFrom: j.recFrom,
      recTo: j.recTo,
    };
    if (sel === idx) render(); else { renderMoves(sel); renderHistory(sel); }
    return true;
  }

  function pollStatus(idx, ply) {
    var tries = 0;
    var tick = function () {
      tries++;
      fetch(DATA.urls.analyze + "?fen=" + encodeURIComponent(ply.fenBefore))
        .then(function (r) { return r.json(); })
        .then(function (j) { if (!applyStatus(idx, ply, j) && tries < 60) setTimeout(tick, 2000); })
        .catch(function () { if (tries < 60) setTimeout(tick, 3000); });
    };
    setTimeout(tick, 2000);
  }

  // ---- events ------------------------------------------------------------
  var navButtons = document.querySelectorAll("#gr-root [data-nav]");
  for (var i = 0; i < navButtons.length; i++) {
    navButtons[i].addEventListener("click", function () {
      var k = this.getAttribute("data-nav");
      if (k === "first") select(0);
      else if (k === "prev") step(-1);
      else if (k === "next") step(1);
      else if (k === "last") select(plies.length);
    });
  }

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
    else if (e.key === "End") { select(plies.length); }
  });

  render();
})();
