"""Unit tests for the AI coach service.

Both external dependencies are mocked: the LC0 UCI engine (reached via
``asyncio`` connection) and the Ollama LLM client. Real ``python-chess``
score objects drive the evaluation-text branches.
"""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import chess
from chess.engine import Cp, Mate, PovScore

from chessdotcom_ai_coach.services import coach

START_FEN = chess.STARTING_FEN
E2E4 = chess.Move.from_uci("e2e4")  # legal in the start position -> SAN "e4"


@contextmanager
def _engine(score, move=E2E4, llm_content="LLM analysis text", llm_raises=False):
    """Patch the engine connection and Ollama client for one call.

    ``score`` is placed in ``result.info["score"]``; ``move`` becomes
    ``result.move``. If ``llm_raises`` the Ollama chat call raises, forcing
    the LC0 fallback branch.
    """
    engine = MagicMock()
    engine.initialize = AsyncMock()
    engine.play = AsyncMock(return_value=SimpleNamespace(move=move, info={"score": score}))
    engine.quit = AsyncMock()

    # The code builds a real UciProtocol and drives it over a socket opened by
    # create_connection; both are stubbed here so no network I/O happens.
    loop = MagicMock()
    loop.create_connection = AsyncMock(return_value=(MagicMock(), MagicMock()))

    ollama_client = MagicMock()
    if llm_raises:
        ollama_client.chat = AsyncMock(side_effect=RuntimeError("ollama down"))
    else:
        ollama_client.chat = AsyncMock(
            return_value=SimpleNamespace(message=SimpleNamespace(content=llm_content))
        )

    with patch.object(coach.asyncio, "get_running_loop", return_value=loop), patch.object(
        coach.chess.engine, "UciProtocol", return_value=engine
    ), patch.object(coach.ollama, "AsyncClient", return_value=ollama_client):
        yield


class TestEvaluationText:
    """eval_text branches, now surfaced as a structured field."""

    async def test_mate_for_white(self):
        with _engine(PovScore(Mate(3), chess.WHITE), llm_raises=True):
            result = await coach.get_best_move(START_FEN)
        assert result["eval_text"] == "Decisive advantage for White: Mate in 3 moves."
        assert result["eval_cp"] == 10.0

    async def test_mate_for_black(self):
        with _engine(PovScore(Mate(-3), chess.WHITE), llm_raises=True):
            result = await coach.get_best_move(START_FEN)
        assert result["eval_text"] == "Decisive advantage for Black: Mate in 3 moves."
        assert result["eval_cp"] == -10.0

    async def test_white_clearly_better(self):
        with _engine(PovScore(Cp(100), chess.WHITE), llm_raises=True):
            result = await coach.get_best_move(START_FEN)
        assert result["eval_text"] == "White is clearly better (+1.00)."
        assert result["eval_cp"] == 1.0

    async def test_black_clearly_better(self):
        with _engine(PovScore(Cp(-100), chess.WHITE), llm_raises=True):
            result = await coach.get_best_move(START_FEN)
        assert result["eval_text"] == "Black is clearly better (-1.00)."

    async def test_balanced_position(self):
        with _engine(PovScore(Cp(0), chess.WHITE), llm_raises=True):
            result = await coach.get_best_move(START_FEN)
        assert result["eval_text"] == "The position is balanced (+0.00)."

    async def test_score_unavailable(self):
        with _engine(None, llm_raises=True):
            result = await coach.get_best_move(START_FEN)
        assert result["eval_text"] == "Analysis unavailable."
        assert result["eval_cp"] is None


class TestBestMoveAndLLM:
    async def test_returns_llm_content_on_success(self):
        with _engine(PovScore(Cp(30), chess.WHITE), llm_content="Play e4, it's great!"):
            result = await coach.get_best_move(START_FEN)
        assert result["analysis"] == "Play e4, it's great!"
        assert result["best_move_san"] == "e4"
        assert result["best_move_uci"] == "e2e4"

    async def test_fallback_mentions_lc0_and_san_when_llm_fails(self):
        with _engine(PovScore(Cp(30), chess.WHITE), llm_raises=True):
            result = await coach.get_best_move(START_FEN)
        assert "LC0" in result["analysis"]
        assert result["best_move_san"] == "e4"  # SAN of the suggested move

    async def test_no_best_move_identified(self):
        with _engine(PovScore(Cp(30), chess.WHITE), move=None):
            result = await coach.get_best_move(START_FEN)
        assert "No clear best move identified." in result["analysis"]
        assert result["best_move_san"] is None


class TestErrorHandling:
    async def test_invalid_fen_returns_error_string(self):
        with _engine(PovScore(Cp(0), chess.WHITE)):
            result = await coach.get_best_move("not-a-valid-fen")
        assert result["analysis"].startswith("Error during LC0 analysis:")
