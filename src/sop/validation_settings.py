"""`validation_settings`(STATE_SCHEMA.md 9번 절): 어느 agent를 어느 검증agent가 언제 검증하는지.

기록이 "검증 대기"인지와 신호를 받을 검증agent의 채널은 이 표에서 정한다. 검증agent 코드는 edge를 하드코딩하지 않고 이 표를 읽는다.
"""

from .state import ValidationSetting

FORECAST_ROLE_TAG = "forecast"
SUPPLY_COORDINATION_ROLE_TAG = "supply_coordination"
FORECAST_VALIDATOR_ROLE_TAG = "forecast_validation"
ALLOCATION_VALIDATOR_ROLE_TAG = "allocation_validation"


def default_validation_settings() -> list[ValidationSetting]:
    """지금 행: forecast → `forecast_validation`, supply_coordination → `allocation_validation`(M4는 회의가 없어 둘 다 `per_record`)."""
    return [
        ValidationSetting(role_tag=FORECAST_ROLE_TAG, validator_role_tag=FORECAST_VALIDATOR_ROLE_TAG, validation_point="per_record"),
        ValidationSetting(
            role_tag=SUPPLY_COORDINATION_ROLE_TAG, validator_role_tag=ALLOCATION_VALIDATOR_ROLE_TAG, validation_point="per_record"
        ),
    ]


def validator_of(settings: list[ValidationSetting], role_tag: str) -> str | None:
    """agent의 기록을 검증하는 검증agent의 role_tag. 검증 대상이 아니면 None."""
    return next((s.validator_role_tag for s in settings if s.role_tag == role_tag), None)
