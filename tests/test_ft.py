"""FT parser tests using markup captured from markets.ft.com (Oct 2026)."""
from app.services import ft_parse_history, ft_parse_tearsheet, is_ft_symbol

TEARSHEET = ('<div data-mod-config="{&quot;xid&quot;:&quot;543017012&quot;,&quot;symbol&quot;:&quot;GB00B0ZDNB53:GBP&quot;}"></div>'
             '<li><span class="mod-ui-data-list__label" title="Net asset value...">Price (GBP)</span>'
             '<span class="mod-ui-data-list__value">12.65</span></li>')
ROWS = ('<tr><td class="mod-ui-table__cell--text"><span class="mod-ui-hide-small-below">Monday, October 12, 2026</span>'
        '<span class="mod-ui-hide-medium-above">Mon, Oct 12, 2026</span></td><td>12.65</td><td>12.65</td><td>12.65</td>'
        '<td>12.65</td><td><span class="mod-ui-hide-small-below">0</span><span class="mod-ui-hide-medium-above">0.00</span></td></tr>'
        '<tr><td class="mod-ui-table__cell--text"><span class="mod-ui-hide-small-below">Friday, October 09, 2026</span>'
        '<span class="mod-ui-hide-medium-above">Fri, Oct 09, 2026</span></td><td>12.62</td><td>12.62</td><td>12.62</td>'
        '<td>12.62</td><td><span class="mod-ui-hide-small-below">0</span><span class="mod-ui-hide-medium-above">0.00</span></td></tr>'
        '<tr><td class="mod-ui-table__cell--text"><span class="mod-ui-hide-small-below">Tuesday, October 01, 2024</span>'
        '<span class="mod-ui-hide-medium-above">Tue, Oct 01, 2024</span></td><td>8.98</td><td>8.98</td><td>8.98</td><td>1,008.98</td>'
        '<td><span class="mod-ui-hide-small-below">0</span></td></tr>')


def test_symbol_detection():
    assert is_ft_symbol("GB00B0ZDNB53") and is_ft_symbol("GB00B0ZDNB53:GBP") and is_ft_symbol("ft:gb00b0zdnb53")
    assert not is_ft_symbol("LQQ3.L") and not is_ft_symbol("VUAG.L") and not is_ft_symbol("")


def test_tearsheet():
    assert ft_parse_tearsheet(TEARSHEET) == ("543017012", "GBP", 12.65)


def test_history():
    assert ft_parse_history(ROWS) == [("2026-10-12", 12.65), ("2026-10-09", 12.62), ("2024-10-01", 1008.98)]


if __name__ == "__main__":
    test_symbol_detection(); test_tearsheet(); test_history(); print("FT parser tests passed")
