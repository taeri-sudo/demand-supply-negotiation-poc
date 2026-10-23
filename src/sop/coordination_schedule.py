"""정해진 조율 시각(하루 한 번, 설정값)과 시한의 기본값 (GRAPH_FLOW.md "조율 흐름", STATE_SCHEMA.md 7번 절).

시한의 길이는 설정값이고 기본값은 다음 정해진 조율 시각이다. 정기 조율과 escalation 시한 초과 신호는 스케줄러가 같은 시각을 기준으로 보낸다.
"""

from datetime import datetime, time, timedelta, timezone

DAILY_COORDINATION_TIME = time(9, 0, tzinfo=timezone.utc)  # 하루 한 번 정해진 조율 시각 (초기값)


def next_daily_coordination(now: datetime, at: time = DAILY_COORDINATION_TIME) -> datetime:
    """`now` 뒤의 첫 정해진 조율 시각. 정확히 그 시각이면 다음 날이다."""
    candidate = datetime.combine(now.astimezone(at.tzinfo).date(), at)
    return candidate if candidate > now else candidate + timedelta(days=1)
