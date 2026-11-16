"""Synthetic native-text examples, never live service answers or generator truth."""

import pytest

from tools.build_executive_demo import answer_rows

TEXT = """SYNTHETIC UNIT ONLY
| Account ID | Account Key | Common service month | Active Lines EOM | Recognized Service Revenue | Contribution Margin |
|---|---:|---|---:|---:|---:|
| `SYNTHETIC_ACCOUNT` | 7 | 2026-07-01 | 0 | 321 | -19 |

Review only; not a billing diagnosis.
"""


def test_observed_native_markdown_shape_preserves_original_integer_values():
    row, = answer_rows(TEXT)
    assert (row.account_id, row.account_key) == ("SYNTHETIC_ACCOUNT", 7)
    assert (row.active_lines_eom, row.recognized_service_revenue_cents, row.contribution_margin_cents) == (0, 321, -19)


@pytest.mark.parametrize("changed", [
    TEXT + TEXT,
    TEXT.replace(" | 0 |", " | missing |"),
    TEXT.replace(" | 321 |", " | 321 cents |"),
    TEXT.replace(" | -19 |", " | -19.0 |"),
    TEXT.replace("2026-07-01", "2026-08-01"),
    TEXT.replace("Contribution Margin", "Calculated New Profit"),
    TEXT.replace("`SYNTHETIC_ACCOUNT`", ""),
    TEXT.replace(" | 321 |", " | 9007199254740993 |"),
])
def test_changed_missing_ambiguous_or_unsafe_native_tables_are_not_invented_answers(changed):
    with pytest.raises(ValueError):
        answer_rows(changed)
