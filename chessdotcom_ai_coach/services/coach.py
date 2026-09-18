import io
import os
from typing import Optional, TypedDict

import chess
import chess.engine
import chess.pgn
from openai import AsyncOpenAI

# Stockfish Engine Configuration.
# The engine runs as a local subprocess inside this container; STOCKFISH_PATH
# points at the binary (see the Dockerfile). Defaults to "stockfish" on PATH.
STOCKFISH_PATH = os.getenv("STOCKFISH_PATH", "stockfish")

# LLM Configuration (OpenRouter)
# OpenRouter is the only supported provider and speaks the OpenAI wire protocol,
# so the standard async client works unchanged. The endpoint is not a knob; the
# model is, since it names a model rather than a provider.
#
# Read straight from the environment, like STOCKFISH_PATH above, so this module
# stays importable without Django configured (see docs/configuration.md).
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Optional: with no key the coach falls back to Stockfish-only prose, same as for
# any other LLM failure. Default to "" rather than None, because
# AsyncOpenAI(api_key=None) silently falls back to the OPENAI_API_KEY environment
# variable, which would pick up an unrelated key on a developer machine.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "anthropic/claude-sonnet-4.5")

# A hosted model answers in seconds, and there are no weights to reload, so 60s is
# a generous cap on a slow response rather than a budget for one.
LLM_TIMEOUT = 60.0


class Suggestion(TypedDict):
    """Structured coach output consumed by the game-detail templates."""

    eval_text: str  # human-readable evaluation, from White's perspective
    eval_cp: Optional[float]  # centipawns (White POV), for the eval bar; None if N/A
    best_move_san: Optional[str]  # recommended move in SAN, e.g. "Nf5"
    best_move_uci: Optional[str]  # recommended move in UCI, e.g. "d4f5" (board highlight)
    analysis: str  # coach prose (LLM, or the Stockfish fallback text)


def _suggestion(
    eval_text: str,
    analysis: str,
    eval_cp: Optional[float] = None,
    best_move_san: Optional[str] = None,
    best_move_uci: Optional[str] = None,
) -> Suggestion:
    return {
        "eval_text": eval_text,
        "eval_cp": eval_cp,
        "best_move_san": best_move_san,
        "best_move_uci": best_move_uci,
        "analysis": analysis,
    }


# Standard piece values, used only to tell the coach who is up material. The
# evaluation itself is Stockfish's job, not this table's.
_PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}


def _ascii_board(board: chess.Board) -> str:
    """Render the board as a labelled grid, uppercase White and lowercase Black.

    The FEN alone is a poor input: a model reconstructing a position from it
    routinely places pieces on squares they are not on, and then explains the
    move in terms of that imagined position. A grid is read far more reliably.
    """
    rows = [
        f"{rank + 1} "
        + " ".join(
            (piece.symbol() if (piece := board.piece_at(chess.square(file, rank))) else ".")
            for file in range(8)
        )
        for rank in range(7, -1, -1)
    ]
    return "\n".join(rows) + "\n  a b c d e f g h"


def _material_balance(board: chess.Board) -> str:
    """Who is up material, in pawns — a fact the coach should not have to infer."""
    totals = {
        color: sum(len(board.pieces(piece, color)) * value for piece, value in _PIECE_VALUES.items())
        for color in (chess.WHITE, chess.BLACK)
    }
    diff = totals[chess.WHITE] - totals[chess.BLACK]
    if diff == 0:
        return "level"
    return f"{'White' if diff > 0 else 'Black'} is up {abs(diff)} (pawns)"


def _history_before(fen: str, pgn: Optional[str]) -> Optional[str]:
    """The moves actually played up to the analysed position, in SAN.

    The callers hand us the game's **whole** PGN — `enqueue_game_analysis` and the
    recovery sweeps queue one task per ply and pass `game.pgn` unchanged — while
    the FEN is the position *before* the move being reviewed. Passing both to the
    model is worse than passing neither: it reads the game's later moves as
    already played, describes a position dozens of plies away from the one it was
    asked about, and gets to see how the game ends.

    So replay the PGN and cut it where the board matches. Comparison is on the
    EPD, not the FEN, because the move counters are not part of the position and
    rows can carry either spelling (see `analysis._rows_by_ply`).

    Returns ``None`` when nothing has been played yet, and also when the position
    is not on the game's main line — an unrelated history is precisely the input
    this exists to remove.
    """
    if not pgn:
        return None
    try:
        game = chess.pgn.read_game(io.StringIO(pgn))
    except Exception:
        return None
    if game is None:
        return None

    target = chess.Board(fen).epd()
    start = game.board()
    if start.epd() == target:
        return None  # the opening position: there is no history to give

    board = start.copy()
    played: list[chess.Move] = []
    for move in game.mainline_moves():
        try:
            board.push(move)
        except Exception:
            break  # malformed movetext — stop at the last legal ply
        played.append(move)
        if board.epd() == target:
            return start.variation_san(played)
    return None


def _bishops(board: chess.Board) -> str:
    """Each side's bishops, with the colour of the square each one stands on.

    The grid says where a bishop is; it does not say what colour it operates on,
    and working that out from the coordinates is exactly the step a model gets
    wrong — calling f8 light-squared while correctly naming the piece. Worth a
    line of its own because so much chess commentary turns on it: good and bad
    bishops, opposite-coloured bishops, the bishop pair.
    """

    def side(color: chess.Color) -> str:
        squares = sorted(board.pieces(chess.BISHOP, color))
        if not squares:
            return "none"
        return ", ".join(
            f"{chess.square_name(square)} "
            f"({'light' if chess.BB_SQUARES[square] & chess.BB_LIGHT_SQUARES else 'dark'})"
            for square in squares
        )

    return f"White {side(chess.WHITE)}; Black {side(chess.BLACK)}"


def _principal_variation(board: chess.Board, info, plies: int = 6) -> Optional[str]:
    """The engine's main line in SAN, e.g. ``4...e5 5. O-O Be7 6. d3``.

    This is the single most useful thing to hand the coach. Given only the best
    move it has to invent a reason the move is good; given the line that follows,
    it can describe what actually happens. Capped at ``plies`` because the tail of
    a 2-second PV is noise.

    Only the longest prefix that actually replays from this position is kept: a
    line cut short still grounds the comment, whereas one that does not fit the
    board is worse than none at all. Returns ``None`` if nothing replays.
    """
    probe = board.copy()
    playable = []
    for move in list(info.get("pv") or ())[:plies]:
        if move not in probe.legal_moves:
            break
        probe.push(move)
        playable.append(move)
    return board.variation_san(playable) if playable else None


async def get_best_move(fen: str, pgn: str | None = None) -> Suggestion:
    """
    Uses the Stockfish engine to act as an AI Chess Coach.
    Returns a structured analysis of the position and a suggested move.

    Args:
        fen: The current position in FEN format.
        pgn: The game history (optional).

    Returns:
        A :class:`Suggestion` dict with the evaluation, the best move (SAN + UCI
        for board highlighting), and the coaching prose.
    """

    try:
        # Launch Stockfish as a local subprocess. popen_uci spawns the process
        # and runs the UCI handshake for us, returning the driver protocol.
        _transport, engine = await chess.engine.popen_uci(STOCKFISH_PATH)

        try:
            board = chess.Board(fen)

            # Analyze to find the best move (2-second limit).
            # play() returns no analysis info by default, so ask for the score and
            # the principal variation explicitly; result.info then carries both.
            # The PV is what lets the coach describe the plan that follows the
            # move instead of guessing at one.
            result = await engine.play(
                board,
                chess.engine.Limit(time=2.0),
                info=chess.engine.INFO_SCORE | chess.engine.INFO_PV,
            )
            best_move = result.move
            info = result.info
        finally:
            # Always terminate the engine subprocess, even on error.
            await engine.quit()

        score = info.get("score")
        eval_cp: float | None = None
        if score is None:
            eval_text = "Analysis unavailable."
        else:
            # Determine the game situation from White's perspective
            white_score = score.white()
            if white_score.is_mate():
                mate_in = white_score.mate()
                if mate_in is not None and mate_in > 0:
                    eval_text = f"Decisive advantage for White: Mate in {mate_in} moves."
                    eval_cp = 10.0  # peg the eval bar to the winning side
                else:
                    eval_text = (
                        f"Decisive advantage for Black: Mate in {abs(mate_in) if mate_in is not None else '?'} moves."
                    )
                    eval_cp = -10.0
            else:
                # Convert the score to centipawns (cp)
                cp = white_score.score(mate_score=10000)
                score_val = cp / 100.0
                eval_cp = score_val
                if score_val > 0.7:
                    eval_text = f"White is clearly better ({score_val:+.2f})."
                elif score_val < -0.7:
                    eval_text = f"Black is clearly better ({score_val:+.2f})."
                else:
                    eval_text = f"The position is balanced ({score_val:+.2f})."

        if best_move is None:
            return _suggestion(
                eval_text=eval_text,
                eval_cp=eval_cp,
                analysis=f"Analysis: {eval_text}\nNo clear best move identified.",
            )

        # Convert the suggested move to Standard Algebraic Notation (SAN)
        best_move_san = board.san(best_move)
        best_move_uci = best_move.uci()

        # Ask the LLM for the coaching prose.
        #
        # Everything the model could otherwise only guess at is spelled out: the
        # board as a grid (not just a FEN), whose turn it is, the material count,
        # the castling rights, and above all the engine's main line. Given only a
        # move and an evaluation, a model invents a justification — pawns on
        # squares they are not on, plans the position does not allow — and the
        # prose reads fluently while being wrong about the board in front of it.
        pv_san = _principal_variation(board, info)
        history_san = _history_before(fen, pgn)
        prompt = f"""
You are a Grandmaster AI Chess Coach. Comment on the following position and on the move the engine recommends.

Position:
- Side to move: {"White" if board.turn == chess.WHITE else "Black"}
- Board (uppercase = White, lowercase = Black, "." = empty):
{_ascii_board(board)}
- FEN: {fen}
- Castling rights: {board.fen().split()[2]}
- Material: {_material_balance(board)}
- Bishops: {_bishops(board)}
- Moves played so far: {history_san if history_san else "none — this is the start of the game"}

Engine analysis:
- Evaluation: {eval_text}
- Best move: {best_move_san}
- Main line: {pv_san if pv_san else "not available"}

Instructions:
1. Briefly comment on the evaluation of the position.
2. Explain why {best_move_san} is the best move in strategic or tactical terms. When a main line is given, use it: describe what actually follows rather than a plan of your own.
3. Provide a short piece of advice for the continuation of the game.
4. Ground every claim in the board above. Do not refer to pawns or pieces on squares where this position does not have them, do not name an opening unless the moves listed support it, and do not describe plans the position does not allow. If the main line is not available and you cannot justify the move concretely, keep the comment general and say the engine prefers it — never invent a reason.
5. Respond in a professional, encouraging, and educational manner in English.
6. Do NOT end your response with a question or an invitation to reply (e.g. "Shall we proceed?"). The interface only offers a "Re-analyze" button, so the user cannot answer. Close with a concise, self-contained statement.
"""

        try:
            # Any failure here — no key at all, a 401, a 429, a timeout, a
            # network error — falls back to engine-only text below. The analysis
            # degrades, it never fails.
            #
            # Checked rather than left to the API so the log line names the cause:
            # an empty key would otherwise surface as an opaque 401.
            if not OPENROUTER_API_KEY:
                raise RuntimeError(
                    "OPENROUTER_API_KEY is not set; skipping the LLM and using "
                    "Stockfish-only prose. See docs/configuration.md#the-api-key."
                )
            # `async with`: the client owns an httpx connection pool that must be
            # closed on this event loop. `async_to_sync` (how the Celery task calls
            # us) closes the loop as soon as we return, so a client left to its
            # finalizer tries to close its socket on a dead loop and logs
            # "RuntimeError: Event loop is closed" after an otherwise fine analysis.
            async with AsyncOpenAI(
                base_url=OPENROUTER_BASE_URL,
                api_key=OPENROUTER_API_KEY,
                timeout=LLM_TIMEOUT,
                # Attributes the requests to this app on OpenRouter's public
                # rankings page. Cosmetic, and it carries no user data.
                default_headers={"X-Title": "chessdotcom_ai_coach"},
            ) as client:
                response = await client.chat.completions.create(
                    model=LLM_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are an expert chess coach analyzing games in real-time. "
                                "Describe only what is present in the position you are given: "
                                "a confident claim about a piece that is not there is worse than "
                                "a general comment. Never end your reply with a question or a "
                                "call to respond; the user has no way to answer back."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.7,
                )
            content = response.choices[0].message.content
            analysis = content.strip() if content else eval_text
            return _suggestion(
                eval_text=eval_text,
                eval_cp=eval_cp,
                best_move_san=best_move_san,
                best_move_uci=best_move_uci,
                analysis=analysis,
            )
        except Exception as llm_err:
            print(f"LLM Error: {llm_err}")

        # Fallback response if LLM is disabled or fails
        fallback = f"""
Here is the analysis from your Grandmaster AI Coach (based on Stockfish):

1. Evaluation: {eval_text}
2. Best Move: {best_move_san}
3. Note: The advanced LLM analysis service is currently unavailable, but Stockfish recommends this move to maintain positional advantage.
""".strip()
        return _suggestion(
            eval_text=eval_text,
            eval_cp=eval_cp,
            best_move_san=best_move_san,
            best_move_uci=best_move_uci,
            analysis=fallback,
        )

    except Exception as e:
        # Error handling (e.g. binary not found, permission denied, UCI error)
        return _suggestion(
            eval_text="Analysis unavailable.",
            analysis=f"Error during Stockfish analysis: {str(e)}",
        )
