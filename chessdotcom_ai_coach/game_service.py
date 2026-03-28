import os
import chess
import chess.engine
import ollama

LC0_PATH = "libs/lc0/lc0"

# LLM Configuration (Local Llama 3 via Ollama)
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL")
OLLAMA_HOST = os.getenv("OLLAMA_HOST")


async def get_best_move(fen: str, pgn: str | None = None) -> str:
    """
    Uses the LC0 engine to act as an AI Chess Coach.
    Returns an analysis of the position and suggests a move.

    Args:
        fen: The current position in FEN format.
        pgn: The game history (optional).

    Returns:
        A string containing the analysis and move suggestion.
    """

    try:
        # Start the LC0 engine in asynchronous mode
        transport, engine = await chess.engine.popen_uci(LC0_PATH)

        board = chess.Board(fen)

        # Analyze to find the best move (2-second limit)
        # PlayResult contains the .info attribute with analysis data (score, depth, etc.)
        result = await engine.play(board, chess.engine.Limit(time=2.0))
        best_move = result.move
        info = result.info

        # Properly close the engine
        await engine.quit()

        score = info.get("score")
        if score is None:
            eval_text = "Analysis unavailable."
        else:
            # Determine the game situation from White's perspective
            white_score = score.white()
            if white_score.is_mate():
                mate_in = white_score.mate()
                if mate_in is not None and mate_in > 0:
                    eval_text = f"Decisive advantage for White: Mate in {mate_in} moves."
                else:
                    eval_text = (
                        f"Decisive advantage for Black: Mate in {abs(mate_in) if mate_in is not None else '?'} moves."
                    )
            else:
                # Convert the score to centipawns (cp)
                cp = white_score.score(mate_score=10000)
                score_val = cp / 100.0
                if score_val > 0.7:
                    eval_text = f"White is clearly better ({score_val:+.2f})."
                elif score_val < -0.7:
                    eval_text = f"Black is clearly better ({score_val:+.2f})."
                else:
                    eval_text = f"The position is balanced ({score_val:+.2f})."

        if best_move is None:
            return f"Analysis: {eval_text}\nNo clear best move identified."

        # Convert the suggested move to Standard Algebraic Notation (SAN)
        best_move_san = board.san(best_move)

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
"""

        try:
            client = ollama.AsyncClient(host=OLLAMA_HOST, timeout=30.0)
            response = await client.chat(  # pyright: ignore[reportCallIssue]
                model=OLLAMA_MODEL,  # pyright: ignore[reportArgumentType]
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert chess coach analyzing games in real-time.",
                    },
                    {"role": "user", "content": prompt},
                ],
                options={
                    "temperature": 0.7,
                },
            )
            content = response.message.content
            return content.strip() if content else ""
        except Exception as llm_err:
            print(f"LLM Error: {llm_err}")

        # Fallback response if LLM is disabled or fails
        return f"""
Here is the analysis from your Grandmaster AI Coach (based on LC0):

1. Evaluation: {eval_text}
2. Best Move: {best_move_san}
3. Note: The advanced LLM analysis service is currently unavailable, but LC0 recommends this move to maintain positional advantage.
""".strip()

    except Exception as e:
        # Error handling (e.g. binary not found, permission denied, UCI error)
        return f"Error during LC0 analysis: {str(e)}"
