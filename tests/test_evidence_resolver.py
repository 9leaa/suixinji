from agent.ask_models import QueryUnit
from agent.evidence_resolver import select_evidence_span


def _unit(question: str) -> QueryUnit:
    return QueryUnit(
        id="u1",
        question=question,
        intent="note_lookup",
        evidence_mode="source_quote",
        need_source_evidence=True,
    )


def test_conversation_span_keeps_answer_turn_for_ordinal_recall() -> None:
    text = (
        "user: Brainstorm ideas for work from home jobs for seniors.\n"
        "assistant: 1. Virtual assistant. 2. Online tutor. 3. Freelance writer. "
        "4. Customer support representative. 5. Bookkeeper. 6. Pet sitter. "
        "7. Transcriptionist. 8. Online survey taker."
    )

    span = select_evidence_span(
        _unit("What was the 7th work from home job for seniors you provided?"), text,
    )

    assert "Transcriptionist" in span
    assert "7." in span


def test_conversation_span_keeps_following_recommendation_list() -> None:
    text = (
        "user: Can you recommend some authentic Italian restaurants in Rome?\n"
        "assistant: 1. Roscioli — a romantic Roman restaurant known for its pasta. "
        "2. Armando al Pantheon — classic Roman cuisine."
    )

    span = select_evidence_span(
        _unit("What romantic Italian restaurant in Rome did you recommend?"), text,
    )

    assert "Roscioli" in span


def test_conversation_span_retains_user_asserted_fact() -> None:
    text = (
        "user: Do you have any favorite features for using the Target Cartwheel app? "
        "I redeemed a $5 coupon on coffee creamer last Sunday.\n"
        "assistant: That is a useful coupon."
    )

    span = select_evidence_span(
        _unit("Where did I redeem a $5 coupon on coffee creamer?"), text,
    )

    assert "Target Cartwheel" in span
    assert "$5 coupon on coffee creamer" in span
