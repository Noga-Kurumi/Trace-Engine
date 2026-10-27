"""Civil time intervals from explicit schema identifiers, never user prose."""
from datetime import datetime, time, timedelta

HINTS = {'reciente': 'recent', 'hoy': 'today', 'ayer': 'yesterday', 'anteayer':'previous_day',
         'recent': 'recent', 'today': 'today', 'yesterday': 'yesterday',
         'morning': 'morning', 'afternoon': 'afternoon', 'evening': 'evening',
         'intervalo':'range', 'range':'range'}


def period(hint='recent', *, now=None, start_time=None, end_time=None):
    clock = now if now is not None else datetime.now()
    if hint not in HINTS:
        raise ValueError('Unknown time-window identifier')
    if (start_time is None) != (end_time is None):
        raise ValueError('An explicit interval needs both start_time and end_time')
    if start_time is not None:
        if HINTS[hint] != 'range':
            raise ValueError('Explicit bounds require time_window=intervalo, not a relative day')
        start = datetime.fromisoformat(start_time).timestamp()
        finish = datetime.fromisoformat(end_time).timestamp()
    else:
        mode = HINTS[hint]
        if mode == 'range':
            raise ValueError('time_window=intervalo requires start_time and end_time')
        midnight = datetime.combine(clock.date(), time.min, tzinfo=clock.tzinfo)
        finish = clock.timestamp()
        if mode in ('yesterday', 'previous_day'):
            days = 1 if mode == 'yesterday' else 2
            start = datetime.combine(clock.date() - timedelta(days=days), time.min,
                                     tzinfo=clock.tzinfo).timestamp()
            finish = datetime.combine(clock.date() - timedelta(days=days-1), time.min,
                                      tzinfo=clock.tzinfo).timestamp()
        elif mode == 'today':
            start = midnight.timestamp()
        elif mode == 'recent':
            start = finish - 7200
        else:
            hour = {'morning': 6, 'afternoon': 12, 'evening': 18}[mode]
            start = datetime.combine(clock.date(), time(hour), tzinfo=clock.tzinfo).timestamp()
            finish = max(start, finish)
    if start > finish:
        raise ValueError('start_time must not be after end_time')
    return start, finish, clock


def stamp(timestamp, clock):
    value = datetime.fromtimestamp(timestamp, tz=clock.tzinfo)
    return (value if value.tzinfo else value.astimezone()).isoformat(timespec='seconds')
