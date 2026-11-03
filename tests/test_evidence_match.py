"""Regression cases for the verbatim-quote guard (failure found in the real-model run, see DECISIONS.md).

quote-match v1 compared characters after light normalisation and rejected genuine quotes that differed
only in markdown escapes, a leading '>' or trailing punctuation. v2 compares word sequences.
"""
from app.extract import quote_in

# Real answer fragments (aimode, prompts 009, 010, 011) and the quotes qwen2.5:3b returned for them.
ANSWER_010 = ("Based on the provided search results from early 2026, **Kasparro appears to be missing major AI models "
              "like Anthropic's Claude and Google's Gemini** , which are currently recognized as top-tier competitors.")
ANSWER_009 = ("Key findings regarding prompt techniques and their usage:\n\n- **Prompt Engineering vs. Hacks:** While some "
              "refer to prompt hacks, experts often define effective prompt engineering as structured design")
ANSWER_011 = ("Based on the available information as of July 2026, **[Kasparro AI]** appears to be a legitimate, "
              "specialized startup focused on \"AI Visibility\".")


def test_trailing_punctuation_and_markdown_do_not_block_a_true_quote():
    assert quote_in("**Kasparro appears to be missing major AI models like Anthropic's Claude and Google's Gemini**.",
                    ANSWER_010)


def test_escaped_bullet_does_not_block_a_true_quote():
    assert quote_in("\\- Prompt Engineering vs. Hacks: While some refer to prompt hacks, experts often define", ANSWER_009)


def test_blockquote_prefix_and_link_brackets_do_not_block_a_true_quote():
    assert quote_in(">Based on the available information as of July 2026, **[Kasparro AI]** appears to be a legitimate",
                    ANSWER_011)


def test_paraphrase_is_still_rejected():
    # model output for gemini/003; the answer says "Leaner, founder-led agile platform geared toward ..."
    answer = "| **Maturity & Scale** | Leaner, founder-led agile platform geared toward fast-moving tech brands. |"
    assert not quote_in("It is leaner, founder-led agile platform geared toward fast-moving tech", answer)


def test_inserted_word_is_rejected():
    answer = 'Marketing claims like:\n\n- "Your AI Visibility Score: 82"\n- "Increase AI visibility by 3.4x"'
    assert not quote_in('Marketing claims like "Your AI Visibility Score: 82" and "Increase AI visibility', answer)


def test_short_quotes_are_rejected():
    assert not quote_in("Kasparro is", "Kasparro is great")
