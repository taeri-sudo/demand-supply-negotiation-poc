"""로깅 헬퍼. `logging` 대신 `print()`, 형식은 `[role_tag:함수명] key=value` (CLAUDE.md)."""


def log(role_tag: str, func_name: str, **fields: object) -> None:
    kv = " ".join(f"{key}={value}" for key, value in fields.items())
    message = f"[{role_tag}:{func_name}]"
    if kv:
        message += f" {kv}"
    print(message)
