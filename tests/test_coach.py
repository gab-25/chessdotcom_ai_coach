"""Unit tests for the AI coach service.

Both external dependencies are mocked: the Stockfish UCI engine (launched via
``chess.engine.popen_uci``) and the OpenAI-compatible LLM client (OpenRouter).
Real ``python-chess`` score objects drive the evaluation-text branches — no test
here reaches the network.
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
def _engine(
    score, move=E2E4, llm_content="LLM analysis text", llm_raises=False, api_key="sk-or-test"
):
    """Patch the engine subprocess and OpenAI-compatible LLM client for one call.

    ``score`` is placed in ``result.info["score"]``; ``move`` becomes
    ``result.move``. If ``llm_raises`` the chat-completions call raises, forcing
    the Stockfish fallback branch. ``api_key`` is patched onto the module because
    it is read at import time; set it to "" to exercise the no-key path.
    """
    engine = MagicMock()
    engine.play = AsyncMock(return_value=SimpleNamespace(move=move, info={"score": score}))
    engine.quit = AsyncMock()

    # The code launches Stockfish via popen_uci, which returns (transport,
    # engine); stubbed here so no real subprocess is spawned.
    popen_uci = AsyncMock(return_value=(MagicMock(), engine))

    # OpenRouter is reached through the OpenAI async client, entered as an async
    # context manager (it owns an httpx pool that must close on this event loop),
    # so the mock has to yield itself from `__aenter__`. The response shape is
    # ``response.choices[0].message.content``.
    llm_client = MagicMock()
    llm_client.__aenter__ = AsyncMock(return_value=llm_client)
    llm_client.__aexit__ = AsyncMock(return_value=False)
    if llm_raises:
        llm_client.chat.completions.create = AsyncMock(side_effect=RuntimeError("llm down"))
    else:
        llm_client.chat.completions.create = AsyncMock(
            return_value=SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=llm_content))]
            )
        )

    with patch.object(coach.chess.engine, "popen_uci", popen_uci), patch.object(
        coach, "OPENROUTER_API_KEY", api_key
    ), patch.object(coach, "AsyncOpenAI", return_value=llm_client) as async_openai:
        yield async_openai


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

    async def test_fallback_mentions_stockfish_and_san_when_llm_fails(self):
        with _engine(PovScore(Cp(30), chess.WHITE), llm_raises=True):
            result = await coach.get_best_move(START_FEN)
        assert "Stockfish" in result["analysis"]
        assert result["best_move_san"] == "e4"  # SAN of the suggested move

    async def test_no_api_key_falls_back_without_calling_the_api(self):
        """The key is optional: with none, the coach never leaves the machine."""
        with _engine(PovScore(Cp(30), chess.WHITE), api_key="") as async_openai:
            result = await coach.get_best_move(START_FEN)
        async_openai.assert_not_called()
        assert "Stockfish" in result["analysis"]
        assert result["best_move_san"] == "e4"

    async def test_client_is_built_against_openrouter(self):
        """The endpoint is pinned and the key is the one from the environment."""
        with _engine(PovScore(Cp(30), chess.WHITE), api_key="sk-or-test") as async_openai:
            await coach.get_best_move(START_FEN)
        kwargs = async_openai.call_args.kwargs
        assert kwargs["base_url"] == "https://openrouter.ai/api/v1"
        assert kwargs["api_key"] == "sk-or-test"  # the key from the environment
        assert kwargs["default_headers"] == {"X-Title": "chessdotcom_ai_coach"}

    async def test_no_best_move_identified(self):
        with _engine(PovScore(Cp(30), chess.WHITE), move=None):
            result = await coach.get_best_move(START_FEN)
        assert "No clear best move identified." in result["analysis"]
        assert result["best_move_san"] is None


class TestErrorHandling:
    async def test_invalid_fen_returns_error_string(self):
        with _engine(PovScore(Cp(0), chess.WHITE)):
            result = await coach.get_best_move("not-a-valid-fen")
        assert result["analysis"].startswith("Error during Stockfish analysis:")
