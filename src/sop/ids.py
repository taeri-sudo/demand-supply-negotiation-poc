"""ID·타임스탬프 헬퍼. 형식 규칙은 CLAUDE.md 참고."""

import uuid
from datetime import datetime, timezone


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
