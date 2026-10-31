import json
from datetime import date

from src.data.nse_calendar import (
    SOURCE_CALENDAR_FILE,
    SOURCE_FIXED,
    calendar_file_covers_year,
    holidays_for_year,
    is_nse_trading_day,
    load_extra_holiday_entries,
    load_extra_holidays,
    previous_nse_trading_day,
)


def test_weekend_is_never_a_trading_day():
    assert is_nse_trading_day(date(2026, 9, 19)) is False  # Saturday
    assert is_nse_trading_day(date(2026, 9, 20)) is False  # Sunday


def test_ordinary_weekday_is_a_trading_day():
    assert is_nse_trading_day(date(2026, 9, 16)) is True  # Wednesday


def test_fixed_date_holiday_is_never_a_trading_day():
    assert is_nse_trading_day(date(2026, 8, 15)) is False  # Independence Day
    assert is_nse_trading_day(date(2026, 1, 26)) is False  # Republic Day


def test_extra_holiday_override_excludes_that_date():
    extra = frozenset({date(2026, 9, 16)})
    assert is_nse_trading_day(date(2026, 9, 16), extra_holidays=extra) is False
    assert is_nse_trading_day(date(2026, 9, 16), extra_holidays=frozenset()) is True


def test_previous_nse_trading_day_skips_weekend():
    # Monday 2026-09-21 -> previous trading day is Friday 2026-09-18
    assert previous_nse_trading_day(date(2026, 9, 21)) == date(2026, 9, 18)


def test_previous_nse_trading_day_skips_fixed_holiday_and_weekend():
    # 2026-08-17 is a Monday; 2026-08-15 (Sat) is Independence Day AND a
    # weekend already, 2026-08-16 is Sunday -- previous trading day is
    # Friday 2026-08-14.
    assert previous_nse_trading_day(date(2026, 8, 17)) == date(2026, 8, 14)


def test_ambedkar_jayanti_is_a_fixed_holiday_every_year():
    assert is_nse_trading_day(date(2026, 4, 14), extra_holidays=frozenset()) is False  # Tuesday
    assert is_nse_trading_day(date(2025, 4, 14), extra_holidays=frozenset()) is False  # Monday


def test_fixed_holidays_are_generated_well_past_2030():
    # The range used to stop at 2030, after which Republic Day was silently a trading day.
    assert is_nse_trading_day(date(2035, 1, 26), extra_holidays=frozenset()) is False  # Friday
    assert is_nse_trading_day(date(2040, 12, 25), extra_holidays=frozenset()) is False  # Tuesday


def _write(path, holidays):
    path.write_text(json.dumps({"holidays": holidays}), encoding="utf-8")


def test_the_calendar_file_takes_bare_dates_and_named_entries(tmp_path):
    calendar = tmp_path / "holidays.json"
    _write(calendar, ["2027-03-22", {"date": "2027-11-09", "name": "Diwali-Balipratipada"}])

    assert load_extra_holiday_entries(calendar) == {
        date(2027, 3, 22): None,
        date(2027, 11, 9): "Diwali-Balipratipada",
    }
    assert load_extra_holidays(calendar) == frozenset({date(2027, 3, 22), date(2027, 11, 9)})


def test_a_missing_calendar_file_is_an_empty_calendar_not_an_error(tmp_path):
    assert load_extra_holiday_entries(tmp_path / "absent.json") == {}
    assert calendar_file_covers_year(2027, path=tmp_path / "absent.json") is False


def test_calendar_file_covers_year_says_which_years_the_file_has_anything_for(tmp_path):
    calendar = tmp_path / "holidays.json"
    _write(calendar, ["2026-03-03", "2026-11-10"])

    assert calendar_file_covers_year(2026, path=calendar) is True
    assert calendar_file_covers_year(2027, path=calendar) is False


def test_holidays_for_year_lists_fixed_and_file_holidays_by_date_with_their_source(tmp_path):
    calendar = tmp_path / "holidays.json"
    _write(calendar, [{"date": "2026-03-03", "name": "Holi"}, "2026-11-10"])

    listed = holidays_for_year(2026, path=calendar)

    assert [holiday.day for holiday in listed] == sorted(holiday.day for holiday in listed)
    by_day = {holiday.day: holiday for holiday in listed}
    assert by_day[date(2026, 3, 3)].name == "Holi"
    assert by_day[date(2026, 3, 3)].source == SOURCE_CALENDAR_FILE
    assert by_day[date(2026, 11, 10)].name is None
    assert by_day[date(2026, 1, 26)].name == "Republic Day"
    assert by_day[date(2026, 1, 26)].source == SOURCE_FIXED
    assert by_day[date(2026, 8, 15)].on_weekend is True  # a Saturday: listed, flagged
    assert by_day[date(2026, 1, 26)].on_weekend is False
    assert all(holiday.day.year == 2026 for holiday in listed)


def test_a_date_in_both_the_fixed_list_and_the_file_is_listed_once(tmp_path):
    calendar = tmp_path / "holidays.json"
    _write(calendar, [{"date": "2026-05-01", "name": "Maharashtra Day (from the file)"}])

    listed = [h for h in holidays_for_year(2026, path=calendar) if h.day == date(2026, 5, 1)]

    assert len(listed) == 1
    assert listed[0].source == SOURCE_FIXED
