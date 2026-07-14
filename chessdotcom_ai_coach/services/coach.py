from django.conf import settings
from ollama import Client


def get_best_move(fen: str, pgn: str | None = None) -> str:
    """
    Uses the official Ollama library to act as a Chess AI Coach.
    Returns an analysis of the position and suggests a move.

    Args:
        fen: The current position in FEN format.
        pgn: The game history (optional).

    Returns:
        A string containing the analysis and move suggestion.
    """
    prompt = f"""
    You are an expert chess instructor (Grandmaster AI Coach).
    Analyze the following FEN position: {fen}
    {"Game history (PGN): " + pgn if pgn else ""}

    Please:
    1. Briefly evaluate who is at an advantage.
    2. Identify the main threat or strategic plan.
    3. Suggest the best move (in algebraic notation, e.g.: e4, Nf3, O-O).
    4. Briefly explain the reasoning behind the move.

    Respond in Italian in a concise and professional manner.
    """

    try:
        # Synchronous client — called from a standard (sync) Django view.
        client = Client(host=settings.OLLAMA_HOST)
        response = client.generate(
            model=settings.OLLAMA_MODEL,
            prompt=prompt,
            options={
                "temperature": 0.3,
            },
        )
        return response.get("response", "Sorry, I cannot analyze the position at the moment.")

    except Exception as e:
        # Generic error handling (connection, timeout, model not found)
        return f"Error during Ollama analysis: {str(e)}"
