/**
 * Chess Board Logic for Custom PNG Assets
 */

class ChessBoard {
  constructor(containerId, options = {}) {
    this.container = document.getElementById(containerId);
    this.fen = options.position || "8/8/8/8/8/8/8/8";
    this.orientation = options.orientation || "white";
    this.pieceTheme =
      options.pieceTheme ||
      ((piece) => `/static/img/${piece.toLowerCase()}.png`);

    this.init();
    this.render();
  }

  init() {
    // Ensure container has correct classes and styles
    this.container.classList.add("chess-board", "relative", "overflow-hidden");
    this.container.style.display = "grid";
    this.container.style.gridTemplateColumns = "repeat(8, 1fr)";
    this.container.style.gridTemplateRows = "repeat(8, 1fr)";
  }

  parseFen(fen) {
    const boardPart = fen.split(" ")[0];
    const rows = boardPart.split("/");
    const board = [];

    rows.forEach((row) => {
      const boardRow = [];
      for (let char of row) {
        if (isNaN(char)) {
          boardRow.push(char);
        } else {
          for (let i = 0; i < parseInt(char); i++) {
            boardRow.push(null);
          }
        }
      }
      board.push(boardRow);
    });

    return board;
  }

  render() {
    this.container.innerHTML = "";
    const boardData = this.parseFen(this.fen);

    // Determine row and column indices based on orientation
    const rowIndices =
      this.orientation === "white"
        ? [0, 1, 2, 3, 4, 5, 6, 7]
        : [7, 6, 5, 4, 3, 2, 1, 0];

    const colIndices =
      this.orientation === "white"
        ? [0, 1, 2, 3, 4, 5, 6, 7]
        : [7, 6, 5, 4, 3, 2, 1, 0];

    rowIndices.forEach((r) => {
      colIndices.forEach((c) => {
        const piece = boardData[r][c];
        const square = document.createElement("div");
        square.className =
          "flex items-center justify-center relative w-full h-full";

        if (piece) {
          const color = piece === piece.toUpperCase() ? "w" : "b";
          const type = piece.toLowerCase();
          const img = document.createElement("img");
          img.src = this.pieceTheme(color + type);
          img.className =
            "w-[90%] h-[90%] object-contain select-none pointer-events-none piece-img";
          img.alt = piece;
          square.appendChild(img);
        }

        this.container.appendChild(square);
      });
    });
  }

  setPosition(fen) {
    this.fen = fen;
    this.render();
  }
}

// Global initialization helper
window.initChessBoard = function (id, config) {
  return new ChessBoard(id, config);
};
