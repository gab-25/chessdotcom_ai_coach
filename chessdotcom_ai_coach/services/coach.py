import os
from typing import Optional, TypedDict

import chess
import chess.engine
import ollama

# Stockfish Engine Configuration.
# The engine runs as a local subprocess inside this container; STOCKFISH_PATH
# points at the binary (see the Dockerfile). Defaults to "stockfish" on PATH.
STOCKFISH_PATH = os.getenv("STOCKFISH_PATH", "stockfish")

# LLM Configuration (Local Llama 3 via Ollama)
# The model is fixed here on purpose: it is not user- or env-selectable.
OLLAMA_MODEL = "llama3:8b"
OLLAMA_HOST = os.getenv("OLLAMA_HOST")
OLLAMA_PORT = os.getenv("OLLAMA_PORT")
OLLAMA_URL = f"http://{OLLAMA_HOST}:{OLLAMA_PORT}"


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
            # play() returns no analysis info by default, so request the score
            # explicitly; result.info then carries the evaluation used below.
            result = await engine.play(
                board, chess.engine.Limit(time=2.0), info=chess.engine.INFO_SCORE
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

        # Generate LLM response using Llama 3
        prompt = f"""
You are a Grandmaster AI Chess Coach. Analyze the following position and suggest the best move.

Context:
- FEN: {fen}
- PGN (History): {pgn if pgn else "N/A"}
- Engine Evaluation: {eval_text}
- Suggested Best Move: {best_move_san}

Instructions:
1. Briefly comment on the evaluation of the position.
2. Explain why {best_move_san} is the best move in strategic or tactical terms.
3. Provide a short piece of advice for the continuation of the game.
4. Respond in a professional, encouraging, and educational manner in English.
5. Do NOT end your response with a question or an invitation to reply (e.g. "Shall we proceed?"). The interface only offers a "Re-analyze" button, so the user cannot answer. Close with a concise, self-contained statement.
"""

        try:
            # llama3:8b on CPU can take ~80s to produce a full analysis, so allow
            # a generous timeout; on failure we fall back to the engine-only text.
            client = ollama.AsyncClient(host=OLLAMA_URL, timeout=150.0)
            response = await client.chat(  # pyright: ignore[reportCallIssue]
                model=OLLAMA_MODEL,  # pyright: ignore[reportArgumentType]
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert chess coach analyzing games in real-time. Never end your reply with a question or a call to respond; the user has no way to answer back.",
                    },
                    {"role": "user", "content": prompt},
                ],
                options={
                    "temperature": 0.7,
                },
            )
            content = response.message.content
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
