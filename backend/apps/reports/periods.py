"""
Which day a figure belongs to.

Two decisions live here, and every number in every report depends on both.

**The server's clock, never the till's.** Reporting reads ``server_received_at``
and never ``device_created_at``. A till whose clock is a day out would otherwise
move revenue between days, and the shop would find its Tuesday takings on
Monday with nothing to explain it. The device time is shown *on a sale*, where a
person can see it is the device's; it never buckets one.

**The business's own day, not UTC.** A Nairobi shop's Tuesday runs midnight to
midnight in Nairobi. A report whose day ends at 3am local is one nobody trusts
twice, and a shop that closes at 9pm would see its evening split across two
days. Boundaries come from ``Tenant.timezone``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone as django_timezone

#: Where a business's day starts and ends when nothing else is configured.
DEFAULT_TIMEZONE = "Africa/Nairobi"

#: How a period may be bucketed.
DAY = "day"
WEEK = "week"
MONTH = "month"
GRANULARITIES = (DAY, WEEK, MONTH)


class PeriodError(ValueError):
    """A period that cannot be built, as opposed to one that is empty."""


def tenant_zone(tenant) -> ZoneInfo:
    """The business's own clock.

    Falls back rather than raising on an unknown zone. A settings value that has
    drifted - a zone renamed, a value hand-edited - must not stop a shop reading
    its takings; it should shift the boundary slightly, which is visible, rather
    than break the page, which is not.
    """
    name = getattr(tenant, "timezone", None) or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


@dataclass(frozen=True)
class Period:
    """A half-open window: ``start <= t < end``.

    Half-open so that consecutive periods neither overlap nor leave a gap. A
    closed window would count a sale at midnight twice, and one shifted by a
    second to avoid that would eventually lose one.
    """

    start: datetime
    end: datetime
    label: str
    granularity: str

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "granularity": self.granularity,
            "start": self.start,
            "end": self.end,
        }


def _local_midnight(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=zone)


def day_period(day: date, *, zone: ZoneInfo) -> Period:
    """One business day, midnight to midnight in the shop's own zone."""
    start = _local_midnight(day, zone)
    # Built by adding a day to the *date* rather than 24 hours to the instant,
    # so a daylight-saving change - which Nairobi does not have, but a future
    # market might - shortens or lengthens the day instead of shifting the
    # boundary into the previous one.
    end = _local_midnight(day + timedelta(days=1), zone)
    return Period(start=start, end=end, label=day.isoformat(), granularity=DAY)


def week_period(day: date, *, zone: ZoneInfo) -> Period:
    """The business week containing a day, Monday to Monday.

    Monday because that is how a Kenyan shop's week reads, and because ISO week
    numbers - which a shop owner will eventually compare against - start there.
    """
    monday = day - timedelta(days=day.weekday())
    start = _local_midnight(monday, zone)
    end = _local_midnight(monday + timedelta(days=7), zone)
    year, week, _weekday = monday.isocalendar()
    return Period(
        start=start, end=end, label=f"{year}-W{week:02d}", granularity=WEEK
    )


def month_period(day: date, *, zone: ZoneInfo) -> Period:
    """The calendar month containing a day."""
    first = day.replace(day=1)
    if first.month == 12:
        following = first.replace(year=first.year + 1, month=1)
    else:
        following = first.replace(month=first.month + 1)
    return Period(
        start=_local_midnight(first, zone),
        end=_local_midnight(following, zone),
        label=f"{first.year}-{first.month:02d}",
        granularity=MONTH,
    )


_BUILDERS = {DAY: day_period, WEEK: week_period, MONTH: month_period}


def period_for(tenant, *, granularity: str = DAY, on: date | None = None) -> Period:
    """The period of the given granularity containing a day.

    Defaults to *today in the business's own zone*, which is not the same as
    today in UTC for a shop trading in the evening.
    """
    if granularity not in GRANULARITIES:
        raise PeriodError(f"Granularity must be one of {', '.join(GRANULARITIES)}.")

    zone = tenant_zone(tenant)
    day = on or django_timezone.now().astimezone(zone).date()
    return _BUILDERS[granularity](day, zone=zone)


def periods_between(
    tenant, *, granularity: str, since: date, until: date
) -> list[Period]:
    """Every period of a granularity across a range, oldest first.

    Bounded so a mistyped range cannot ask the database for ten thousand
    buckets. The cap is generous enough for three years of daily figures, which
    is more history than a duka has.
    """
    if granularity not in GRANULARITIES:
        raise PeriodError(f"Granularity must be one of {', '.join(GRANULARITIES)}.")
    if until < since:
        raise PeriodError("The end of a range cannot be before its start.")

    zone = tenant_zone(tenant)
    build = _BUILDERS[granularity]

    periods: list[Period] = []
    cursor = since
    while cursor <= until:
        period = build(cursor, zone=zone)
        if not periods or periods[-1].label != period.label:
            periods.append(period)
        if len(periods) > MAX_PERIODS:
            raise PeriodError(
                f"That range covers more than {MAX_PERIODS} periods. Ask for a "
                "coarser granularity or a shorter range."
            )
        cursor += timedelta(days=1)

    return periods


#: Three years of daily buckets, which is more history than a duka has.
MAX_PERIODS = 1100
