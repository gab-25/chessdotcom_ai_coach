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
    score,
    move=E2E4,
    llm_content="LLM analysis text",
    llm_raises=False,
    api_key="sk-or-test",
    pv=None,
):
    """Patch the engine subprocess and OpenAI-compatible LLM client for one call.

    ``score`` is placed in ``result.info["score"]`` and ``pv`` in
    ``result.info["pv"]``; ``move`` becomes ``result.move``. If ``llm_raises`` the chat-completions call raises, forcing
    the Stockfish fallback branch. ``api_key`` is patched onto the module because
    it is read at import time; set it to "" to exercise the no-key path.
    """
    engine = MagicMock()
    info = {"score": score}
    if pv is not None:
        info["pv"] = pv
    engine.play = AsyncMock(return_value=SimpleNamespace(move=move, info=info))
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
        # HTTP-Referer is the header OpenRouter attributes the app by; X-Title
        # only renames it, so a missing referer files the usage under "Unknown".
        assert kwargs["default_headers"] == {
            "HTTP-Referer": "https://github.com/gab-25/chessdotcom_ai_coach",
            "X-Title": "chessdotcom_ai_coach",
        }

    async def test_no_best_move_identified(self):
        with _engine(PovScore(Cp(30), chess.WHITE), move=None):
            result = await coach.get_best_move(START_FEN)
        assert "No clear best move identified." in result["analysis"]
        assert result["best_move_san"] is None


class TestPromptGrounding:
    """What the model is told about the position.

    Every factual slip in the coach prose traces back to something the prompt did
    not say, so these assert the grounding is actually sent rather than assumed.
    """

    @staticmethod
    def _prompt(async_openai):
        client = async_openai.return_value
        messages = client.chat.completions.create.call_args.kwargs["messages"]
        return next(m["content"] for m in messages if m["role"] == "user")

    async def test_prompt_carries_the_board_not_just_the_fen(self):
        with _engine(PovScore(Cp(30), chess.WHITE)) as async_openai:
            await coach.get_best_move(START_FEN)
        prompt = self._prompt(async_openai)
        assert "8 r n b q k b n r" in prompt  # the back rank, as a labelled grid
        assert "  a b c d e f g h" in prompt
        assert START_FEN in prompt  # the FEN is still there, alongside

    async def test_prompt_carries_side_to_move_and_material(self):
        with _engine(PovScore(Cp(30), chess.WHITE)) as async_openai:
            await coach.get_best_move(START_FEN)
        prompt = self._prompt(async_openai)
        assert "Side to move: White" in prompt
        assert "Material: level" in prompt
        assert "Castling rights: KQkq" in prompt

    async def test_prompt_names_each_bishop_square_colour(self):
        """Where a bishop stands is on the grid; what colour it works on is not."""
        with _engine(PovScore(Cp(30), chess.WHITE)) as async_openai:
            await coach.get_best_move(START_FEN)
        prompt = self._prompt(async_openai)
        assert "Bishops: White c1 (dark), f1 (light); Black c8 (light), f8 (dark)" in prompt

    async def test_bishopless_side_is_stated_not_omitted(self):
        """An endgame with no bishops must read as "none", not as a missing field."""
        fen = "4k3/8/8/8/8/8/8/4K2B w - - 0 1"  # White keeps one, Black has none
        h1 = chess.Move.from_uci("h1g2")
        with _engine(PovScore(Cp(30), chess.WHITE), move=h1) as async_openai:
            await coach.get_best_move(fen)
        assert "Bishops: White h1 (light); Black none" in self._prompt(async_openai)

    async def test_prompt_carries_the_engine_main_line(self):
        """The PV is what stops the coach inventing a plan of its own."""
        pv = [chess.Move.from_uci(u) for u in ("e2e4", "e7e5", "g1f3")]
        with _engine(PovScore(Cp(30), chess.WHITE), pv=pv) as async_openai:
            await coach.get_best_move(START_FEN)
        assert "Main line: 1. e4 e5 2. Nf3" in self._prompt(async_openai)

    async def test_prompt_says_so_when_there_is_no_main_line(self):
        """No PV must read as absent, never as a line the engine did not give."""
        with _engine(PovScore(Cp(30), chess.WHITE)) as async_openai:
            await coach.get_best_move(START_FEN)
        assert "Main line: not available" in self._prompt(async_openai)

    async def test_unplayable_main_line_is_dropped(self):
        """A PV that does not replay from this position is discarded, not sent."""
        illegal = [chess.Move.from_uci("a1a8")]
        with _engine(PovScore(Cp(30), chess.WHITE), pv=illegal) as async_openai:
            await coach.get_best_move(START_FEN)
        assert "Main line: not available" in self._prompt(async_openai)

    async def test_main_line_is_truncated_at_the_first_unplayable_move(self):
        """A good prefix still grounds the comment; the bad tail is cut, not kept."""
        pv = [chess.Move.from_uci(u) for u in ("e2e4", "e7e5", "a1a8")]
        with _engine(PovScore(Cp(30), chess.WHITE), pv=pv) as async_openai:
            await coach.get_best_move(START_FEN)
        prompt = self._prompt(async_openai)
        assert "Main line: 1. e4 e5" in prompt
        assert "a8" not in prompt.split("Main line:")[1].split("\n")[0]


class TestHistoryTruncation:
    """The PGN must be cut at the analysed position.

    Callers pass the game's whole PGN with a FEN from one ply, so an untruncated
    history describes a position the coach was not asked about — and shows it how
    the game ends.
    """

    FULL_PGN = "1. e4 c5 2. Nc3 Nc6 3. Nf3 d6 4. Bc4"

    @staticmethod
    def _fen_after(*sans):
        board = chess.Board()
        for san in sans:
            board.push_san(san)
        return board.fen()

    async def test_history_stops_at_the_analysed_position(self):
        fen = self._fen_after("e4", "c5", "Nc3")  # Black to move at ply 4
        nf6 = chess.Move.from_uci("g8f6")  # legal here, unlike the default e2e4
        with _engine(PovScore(Cp(30), chess.WHITE), move=nf6) as async_openai:
            await coach.get_best_move(fen, self.FULL_PGN)
        prompt = TestPromptGrounding._prompt(async_openai)
        assert "Moves played so far: 1. e4 c5 2. Nc3" in prompt
        assert "Nc6" not in prompt.split("Moves played so far:")[1].split("\n")[0]

    async def test_start_of_game_has_no_history(self):
        with _engine(PovScore(Cp(30), chess.WHITE)) as async_openai:
            await coach.get_best_move(START_FEN, self.FULL_PGN)
        assert "Moves played so far: none" in TestPromptGrounding._prompt(async_openai)

    async def test_position_off_the_main_line_gets_no_history(self):
        """Better no history than one from a game that never reached here."""
        fen = self._fen_after("d4", "d5")  # not a position in FULL_PGN
        c4 = chess.Move.from_uci("c2c4")  # legal here, unlike the default e2e4
        with _engine(PovScore(Cp(30), chess.WHITE), move=c4) as async_openai:
            await coach.get_best_move(fen, self.FULL_PGN)
        assert "Moves played so far: none" in TestPromptGrounding._prompt(async_openai)


class TestPlainTextOutput:
    """The card renders the prose as text, so Markdown reaches the reader raw.

    The prompt forbids it, but an instruction is a request: these cover the reply
    that ignores it.
    """

    async def test_headings_and_emphasis_are_stripped(self):
        reply = "# Analysis\n\n## Evaluation\n\nThe move **e5** is *strong*."
        with _engine(PovScore(Cp(30), chess.WHITE), llm_content=reply):
            result = await coach.get_best_move(START_FEN)
        assert result["analysis"] == "Analysis\n\nEvaluation\n\nThe move e5 is strong."

    async def test_checkmate_notation_survives(self):
        """`#` is checkmate in SAN — only a line-leading `# ` is a heading."""
        reply = "White mates with Qh5#, or Rd8# after Kf1."
        with _engine(PovScore(Cp(30), chess.WHITE), llm_content=reply):
            result = await coach.get_best_move(START_FEN)
        assert result["analysis"] == reply

    async def test_paragraph_breaks_are_preserved(self):
        """The card's CSS renders newlines, so welding paragraphs is a visible bug."""
        reply = "## First\n\nOne.\n\n## Second\n\nTwo."
        with _engine(PovScore(Cp(30), chess.WHITE), llm_content=reply):
            result = await coach.get_best_move(START_FEN)
        assert result["analysis"].count("\n\n") == 3

    async def test_bullet_lists_are_left_alone(self):
        """"- Develop" reads fine as plain text; "**" never does."""
        reply = "Options:\n- Defend with Nc6\n- Counterattack with Nf6"
        with _engine(PovScore(Cp(30), chess.WHITE), llm_content=reply):
            result = await coach.get_best_move(START_FEN)
        assert result["analysis"] == reply


class TestErrorHandling:
    async def test_invalid_fen_returns_error_string(self):
        with _engine(PovScore(Cp(0), chess.WHITE)):
            result = await coach.get_best_move("not-a-valid-fen")
        assert result["analysis"].startswith("Error during Stockfish analysis:")
