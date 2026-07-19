import os
import asyncio
from typing import Optional, TypedDict

import chess
import chess.engine
import ollama

# LC0 Engine Configuration (Remote service)
CHESS_ENGINE_HOST = os.getenv("CHESS_ENGINE_HOST")
CHESS_ENGINE_PORT = int(os.getenv("CHESS_ENGINE_PORT"))  # pyright: ignore[reportArgumentType]

# LLM Configuration (Local Llama 3 via Ollama)
# The model is fixed here on purpose: it is not user- or env-selectable.
OLLAMA_MODEL = "llama3:8b"
OLLAMA_HOST = os.getenv("OLLAMA_HOST")
OLLAMA_PORT = os.getenv("OLLAMA_PORT")
OLLAMA_URL = f"http://{OLLAMA_HOST}:{OLLAMA_PORT}"


class _TcpUciTransport(asyncio.Protocol):
    """Bridge a plain TCP socket to python-chess's subprocess-oriented ``UciProtocol``.

    ``UciProtocol`` is an :class:`asyncio.SubprocessProtocol`: it writes commands to
    the engine's "stdin" through ``transport.get_pipe_transport(0).write()`` and expects
    the engine's output to arrive via ``Protocol.pipe_data_received(fd=1, ...)``. A raw
    socket transport exposes neither, so this adapter sits between the two — presenting
    the ``get_pipe_transport``/``write``/``get_returncode`` surface the engine needs,
    while forwarding incoming socket bytes to the engine as if they were stdout (fd 1).
    """

    def __init__(self, engine: "chess.engine.Protocol") -> None:
        self.engine = engine
        self._transport: Optional[asyncio.Transport] = None
        self._returncode: Optional[int] = None

    # --- asyncio.Protocol callbacks (socket side) ---
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # pyright: ignore[reportAttributeAccessIssue]
        # Hand ourselves to the engine as its "subprocess transport".
        self.engine.connection_made(self)  # pyright: ignore[reportArgumentType]

    def data_received(self, data: bytes) -> None:
        # Engine output is the equivalent of subprocess stdout (fd 1).
        self.engine.pipe_data_received(1, data)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        self._returncode = 0
        self.engine.connection_lost(exc)

    # --- SubprocessTransport surface expected by UciProtocol ---
    def get_pipe_transport(self, fd: int) -> "asyncio.WriteTransport":
        return self  # pyright: ignore[reportReturnType]

    def write(self, data: bytes) -> None:
        assert self._transport is not None
        self._transport.write(data)

    def get_returncode(self) -> Optional[int]:
        return self._returncode

    def get_pid(self) -> int:
        return id(self)


class Suggestion(TypedDict):
    """Structured coach output consumed by the game-detail templates."""

    eval_text: str  # human-readable evaluation, from White's perspective
    eval_cp: Optional[float]  # centipawns (White POV), for the eval bar; None if N/A
    best_move_san: Optional[str]  # recommended move in SAN, e.g. "Nf5"
    best_move_uci: Optional[str]  # recommended move in UCI, e.g. "d4f5" (board highlight)
    analysis: str  # coach prose (LLM, or the LC0 fallback text)


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
    Uses the LC0 engine to act as an AI Chess Coach.
    Returns a structured analysis of the position and a suggested move.

    Args:
        fen: The current position in FEN format.
        pgn: The game history (optional).

    Returns:
        A :class:`Suggestion` dict with the evaluation, the best move (SAN + UCI
        for board highlighting), and the coaching prose.
    """

    try:
        # Connect to the remote LC0 engine service via network.
        # UciProtocol is subprocess-oriented, so we drive it over the TCP socket
        # through _TcpUciTransport instead of handing it to create_connection
        # directly.
        loop = asyncio.get_running_loop()
        engine = chess.engine.UciProtocol()
        await loop.create_connection(
            lambda: _TcpUciTransport(engine), CHESS_ENGINE_HOST, CHESS_ENGINE_PORT  # pyright: ignore[reportArgumentType]
        )

        # create_connection only opens the socket; unlike popen_uci it does not
        # run the UCI handshake, so we must initialize the protocol manually
        # (send "uci" / await "uciok") before issuing any command.
        await engine.initialize()

        board = chess.Board(fen)

        # Analyze to find the best move (2-second limit).
        # play() returns no analysis info by default, so request the score
        # explicitly; result.info then carries the evaluation used below.
        result = await engine.play(
            board, chess.engine.Limit(time=2.0), info=chess.engine.INFO_SCORE
        )
        best_move = result.move
        info = result.info

        # Properly close the engine
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
- LC0 Evaluation: {eval_text}
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
            # a generous timeout; on failure we fall back to the LC0-only text.
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
Here is the analysis from your Grandmaster AI Coach (based on LC0):

1. Evaluation: {eval_text}
2. Best Move: {best_move_san}
3. Note: The advanced LLM analysis service is currently unavailable, but LC0 recommends this move to maintain positional advantage.
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
            analysis=f"Error during LC0 analysis: {str(e)}",
        )
