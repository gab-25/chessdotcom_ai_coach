/**
 * Alpine.js component for Chess Board rendering
 * This replaces the previous class-based implementation with a declarative approach.
 */

document.addEventListener("alpine:init", () => {
  Alpine.data("chessBoard", (config) => ({
    fen: config.position || "8/8/8/8/8/8/8/8",
    orientation: config.orientation || "white",

    /**
     * Computed property to get the 64 squares of the board
     * handles FEN parsing and orientation automatically.
     */
    get squares() {
      const boardPart = this.fen.split(" ")[0];
      const rows = boardPart.split("/");
      const boardMatrix = [];

      // Parse FEN string into an 8x8 matrix
      rows.forEach((row) => {
        const boardRow = [];
        for (let char of row) {
          if (isNaN(char)) {
            boardRow.push(char);
          } else {
            const emptyCount = parseInt(char);
            for (let i = 0; i < emptyCount; i++) {
              boardRow.push(null);
            }
          }
        }
        boardMatrix.push(boardRow);
      });

      const flatSquares = [];
      const isWhite = this.orientation === "white";

      // Row/Column mapping based on orientation
      // White view: Top row is rank 8 (index 0), Bottom is rank 1 (index 7)
      // Black view: Top row is rank 1 (index 7), Bottom is rank 8 (index 0)
      const rowRange = isWhite
        ? [0, 1, 2, 3, 4, 5, 6, 7]
        : [7, 6, 5, 4, 3, 2, 1, 0];
      const colRange = isWhite
        ? [0, 1, 2, 3, 4, 5, 6, 7]
        : [7, 6, 5, 4, 3, 2, 1, 0];

      rowRange.forEach((r) => {
        colRange.forEach((c) => {
          const piece = boardMatrix[r][c];
          if (piece) {
            const color = piece === piece.toUpperCase() ? "w" : "b";
            const type = piece.toLowerCase();
            flatSquares.push({
              name: piece,
              src: `/static/img/${color}${type}.png`,
            });
          } else {
            flatSquares.push(null);
          }
        });
      });

      return flatSquares;
    },
  }));
});
