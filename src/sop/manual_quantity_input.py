"""사람이 정한 수량을 넣는 함수 (AGENT_NODE_LIST.md "외부 경계"의 사람이 정한 수량).

주문이 없는 수량(신제품 첫 물량, 프로모션 이력이 없는 고객사·item의 프로모션 수량, 아직 주문하지 않은 새 고객사의 예상 물량)을 사람이 외부
데이터에 넣는다. 넣을 때 입력 제한으로 확인하고(거부하면 아무것도 바꾸지 않는다), 넣은 뒤 그 item의 라인에 외부 데이터 변경 알림을 보낸다.
알림의 작성자는 값을 넣은 사람(`entered_by`)이라 sales_channel로 남지 않는다. 사람이 정한 수량은 변경으로 읽히면 진행 중인 조율 직후에
조율된다(GRAPH_FLOW.md "이어지는 조율의 시점").
"""

import math

from .access import StateStore
from .ids import new_id
from .line_data_provider import SampleLineDataProvider
from .logging_utils import log
from .supply_allocation_rules import ManualDemand
from .supply_coordination_agent import notify_line_changed

HUMAN_MANAGER = "human_manager"
MANUAL_PREFIX = "MAN"


class ManualInputRejected(ValueError):
    """사람이 정한 수량을 거부함. `code`는 거부 이유(`unknown_item`, `not_a_number`, `negative`, `not_unit_multiple`, `duplicate`)다."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def enter_manual_quantity(
    store: StateStore,
    provider: SampleLineDataProvider,
    *,
    company_id: str | None,
    item_id: str,
    quantity: float,
    entered_by: str = HUMAN_MANAGER,
) -> ManualDemand:
    """사람이 정한 수량 하나를 외부 데이터에 넣고 그 item의 라인에 변경 알림을 보낸다. 입력 제한을 어기면 `ManualInputRejected`다.

    입력 제한: item에 주 라인이 있어야 하고, 숫자이며 0 이상이고, 계약 단위의 배수여야 하며, 같은 (고객사, item)에 같은 수량이 이미 있으면
    중복으로 거부한다. 고객사는 정해지지 않았을 수 있다(None).
    """
    try:
        line_id = provider.line_of(item_id)
    except KeyError as error:
        raise ManualInputRejected("unknown_item", str(error.args[0])) from None
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not math.isfinite(quantity):
        raise ManualInputRejected("not_a_number", f"수량이 숫자가 아님 ({quantity!r})")
    if quantity < 0:
        raise ManualInputRejected("negative", f"수량은 0 이상이어야 함 ({quantity})")
    unit = provider.unit(company_id, item_id)
    if abs(round(quantity / unit) * unit - quantity) > 1e-9:
        raise ManualInputRejected("not_unit_multiple", f"수량이 계약 단위 {unit:g}의 배수가 아님 ({quantity})")
    if any((m.company_id, m.item_id, m.quantity) == (company_id, item_id, float(quantity)) for m in provider.manual_quantities()):
        raise ManualInputRejected("duplicate", f"같은 수량이 이미 있음: {company_id}/{item_id} {quantity:g}")
    demand = ManualDemand(demand_id=new_id(MANUAL_PREFIX), company_id=company_id, item_id=item_id, quantity=float(quantity))
    provider.add_manual(demand)
    notify_line_changed(store, line_id, written_by=entered_by)
    log(entered_by, "enter_manual_quantity", demand_id=demand.demand_id, line_id=line_id, item_id=item_id, quantity=quantity)
    return demand
