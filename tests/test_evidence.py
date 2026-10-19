from src.evidence import EvidenceLocator


def test_exact_quote_found_with_page(genel_document):
    match = EvidenceLocator(genel_document).locate("Issue Amount: USD 300,000,000", claimed_page=1)
    assert match.found and match.page == 1 and match.match_type == "normalized"


def test_typographic_quotes_and_whitespace_are_normalised(genel_document):
    match = EvidenceLocator(genel_document).locate('Day-count fraction for coupon is "30/360"')
    assert match.found and match.page == 1


def test_reading_order_view_matches_table_cell_text(idbi_document):
    # In the layout view the label and value interleave; the reading-order view keeps the cell intact.
    quote = "Rs.1,500 Crores with an option to retain over subscription up to Rs.1,000 Crores."
    match = EvidenceLocator(idbi_document).locate(quote)
    assert match.found and match.page == 1 and match.text_view == "reading_order"


def test_hyphenated_line_break_needs_whitespace_insensitive_match(idbi_document):
    # The PDF breaks "vis-\nà-vis" across lines, which only the whitespace-insensitive tier accepts.
    match = EvidenceLocator(idbi_document).locate("vis-à-vis other creditors of the Issuer")
    assert match.found and match.page == 1
    assert match.match_type == "whitespace_insensitive"


def test_page_is_where_quote_is_found_not_where_llm_claims(idbi_document):
    match = EvidenceLocator(idbi_document).locate("Coupon Rate 10.75% p.a.", claimed_page=9)
    assert match.found and match.page == 3


def test_fabricated_or_paraphrased_quotes_are_rejected(genel_document, idbi_document):
    assert not EvidenceLocator(genel_document).locate("Coupon Rate: 9.50% p.a.").found
    assert not EvidenceLocator(idbi_document).locate("Maturity Date: 17 October 2025").found
    assert not EvidenceLocator(idbi_document).locate("coupon rate 10.75% p.a.").found  # case is not normalised
    assert not EvidenceLocator(idbi_document).locate("   ").found
