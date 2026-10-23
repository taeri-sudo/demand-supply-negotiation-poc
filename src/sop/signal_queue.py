"""확인 응답 방식의 신호 큐 (GRAPH_FLOW.md "동시성 모델" 신호 전달).

받는 쪽이 신호를 꺼내도 큐에서 바로 지우지 않고 "처리 중"으로 둔다. 일을 끝내면(자기 상태가 아니라서 그냥 넘어간 경우 포함) `ack`로
처리했다고 알려 지운다. 정해진 시간(`ack_timeout`) 안에 `ack`가 없으면(처리 도중 실패) 그 신호만 다시 나타난다. 모든 채널이 같은 규칙이다.

신호는 기록의 위치만 담은 초인종이고 받는 쪽이 기록의 상태를 직접 읽어 판단하므로, 같은 신호가 다시 와도 결과가 같다. 시간은 주입한
`clock`(초 단위 단조 시계)로 잰다.
"""

import asyncio
import time
from collections import deque
from collections.abc import Callable
from typing import Any

ACK_TIMEOUT_SECONDS = 30.0  # 확인이 없으면 신호가 다시 나타나는 시간 (초기값)

Signal = dict[str, Any]


class SignalQueue:
    """채널 하나의 큐. `get`/`get_nowait`로 꺼낸 신호는 `ack`하기 전까지 처리 중이고, 시간이 지나면 준비된 신호로 돌아온다.

    `qsize`·`empty`는 준비된 신호만 센다(처리 중인 신호는 빠진다). 신호는 객체 동일성으로 구분한다.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, ack_timeout: float = ACK_TIMEOUT_SECONDS) -> None:
        self._clock = clock
        self._ack_timeout = ack_timeout
        self._ready: deque[Signal] = deque()
        self._in_flight: list[tuple[float, Signal]] = []  # (확인 기한, 신호)
        self._wakeup = asyncio.Event()

    def put_nowait(self, signal: Signal) -> None:
        self._ready.append(signal)
        self._wakeup.set()

    def _requeue_expired(self) -> None:
        now = self._clock()
        expired = [signal for deadline, signal in self._in_flight if deadline <= now]
        if expired:
            self._in_flight = [(d, s) for d, s in self._in_flight if d > now]
            self._ready.extend(expired)
            self._wakeup.set()

    def qsize(self) -> int:
        self._requeue_expired()
        return len(self._ready)

    def empty(self) -> bool:
        return self.qsize() == 0

    def in_flight_count(self) -> int:
        """처리 중(꺼냈지만 아직 확인하지 않은) 신호 수. 확인 기한이 지난 신호는 준비된 신호로 돌아가므로 빠진다."""
        self._requeue_expired()
        return len(self._in_flight)

    def get_nowait(self) -> Signal:
        """준비된 신호 하나를 꺼내 처리 중으로 둔다. 없으면 `asyncio.QueueEmpty`."""
        self._requeue_expired()
        if not self._ready:
            raise asyncio.QueueEmpty
        signal = self._ready.popleft()
        self._in_flight.append((self._clock() + self._ack_timeout, signal))
        return signal

    async def get(self) -> Signal:
        """준비된 신호가 생길 때까지(새 신호나 확인 기한이 지난 신호) 기다려 하나를 꺼낸다."""
        while True:
            try:
                return self.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._wakeup.clear()
            delay = max(0.0, min(d for d, _ in self._in_flight) - self._clock()) if self._in_flight else None
            try:
                await asyncio.wait_for(self._wakeup.wait(), delay)
            except TimeoutError:
                pass

    def ack(self, signal: Signal) -> bool:
        """신호를 처리했다고 알려 지운다. 이미 지워졌으면 False다. 확인이 늦어 다시 나타난 신호가 아직 준비 상태이면 그것도 지운다."""
        for index, (_, pending) in enumerate(self._in_flight):
            if pending is signal:
                del self._in_flight[index]
                return True
        for index, pending in enumerate(self._ready):
            if pending is signal:
                del self._ready[index]
                return True
        return False
