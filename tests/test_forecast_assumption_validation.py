"""가정 검증 조건 — 위반 케이스를 잡아내고, 요청량이 같아도 데이터나 기법이 다르면 다른 가정으로 본다."""

from sop.forecast_assumption_validation import validate_assumptions
from sop.state import Assumption, DataKind, DataSource, Driver, DriverName, Evidence, ItemScope, MethodValue

HISTORY = [100.0, 120.0, 90.0, 110.0, 105.0, 95.0]
SOURCES = [DataSource(kind="market", item_scope="category"), DataSource(kind="orders", item_scope="same_item")]


def driver(
    name: DriverName = "category_trend", kind: DataKind = "market", scope: ItemScope = "category", refs=("D151",)
):
    return Driver(driver=name, evidence=Evidence(kind=kind, item_scope=scope, refs=list(refs)))


def assumption(assumption_id, drivers=(), value=105.0, methods=("regression",)):
    return Assumption(
        assumption_id=assumption_id,
        drivers=list(drivers),
        value=value,
        method_values=[MethodValue(method=m, value=value, method_weight=1 / len(methods)) for m in methods],
    )


def issues(assumptions, sources=SOURCES):
    return {(c.assumption_id, c.issue) for c in validate_assumptions(assumptions, sources, HISTORY)}


def test_valid_assumptions_have_no_violation():
    assert issues([assumption("A-DEFAULT"), assumption("A-TREND", [driver()], methods=("regression", "regression_ar1"))]) == set()


def test_evidence_missing_from_the_snapshot_is_no_evidence():
    only_orders = [DataSource(kind="orders", item_scope="same_item")]
    assert issues([assumption("A-TREND", [driver()])], only_orders) == {("A-TREND", "no_evidence")}


def test_value_far_outside_the_past_range_is_value_out_of_range():
    assert issues([assumption("A-HIGH", value=500.0), assumption("A-NEG", value=-200.0, methods=("naive",))]) == {
        ("A-HIGH", "value_out_of_range"),
        ("A-NEG", "value_out_of_range"),
    }
    assert issues([assumption("A-EDGE", value=135.0)]) == set()  # 범위 폭 30의 0.5배(15)까지는 허용


def test_same_composition_is_not_distinct_even_though_values_differ():
    first = assumption("A-1", [driver()], value=100.0)
    second = assumption("A-2", [driver()], value=130.0)
    assert issues([first, second]) == {("A-2", "not_distinct")}


def test_same_value_but_different_data_or_methods_is_a_different_assumption():
    base = assumption("A-1", [driver()], value=105.0, methods=("regression",))
    other_data = assumption("A-2", [driver(refs=("D152",))], value=105.0, methods=("regression",))
    other_methods = assumption("A-3", [driver()], value=105.0, methods=("regression", "regression_ar1"))
    other_driver = assumption("A-4", [driver(name="event", kind="orders", scope="same_item", refs=("ITEM-1",))], value=105.0)
    assert issues([base, other_data, other_methods, other_driver]) == set()


def test_two_drivers_sharing_the_same_evidence_are_double_counted():
    twin = assumption("A-TWIN", [driver(), driver(name="event")])  # 같은 근거를 두 원인이 중복 사용
    assert issues([twin]) == {("A-TWIN", "double_counted")}


def test_violations_are_returned_as_assumption_type_suspected_causes():
    (cause,) = validate_assumptions([assumption("A-HIGH", value=500.0)], SOURCES, HISTORY)
    assert cause.type == "assumption" and cause.assumption_id == "A-HIGH" and cause.issue == "value_out_of_range"


def causes_of(assumptions, sources=SOURCES):
    return {(c.issue, c.assumption_id): c.drivers for c in validate_assumptions(assumptions, sources, HISTORY)}


def test_no_evidence_points_at_the_drivers_without_evidence_and_not_at_the_others():
    only_orders = [DataSource(kind="orders", item_scope="same_item")]
    mixed = assumption("A-MIX", [driver(), driver(name="event", kind="orders", scope="same_item", refs=("ITEM-1",))])

    assert causes_of([mixed], only_orders) == {("no_evidence", "A-MIX"): ["category_trend"]}


def test_double_counted_points_at_the_drivers_whose_evidence_overlaps():
    twin = assumption("A-TWIN", [driver(), driver(name="event"), driver(name="price", kind="pos", scope="same_item", refs=("X",))])

    with_pos = [*SOURCES, DataSource(kind="pos", item_scope="same_item")]

    assert causes_of([twin], with_pos) == {("double_counted", "A-TWIN"): ["category_trend", "event"]}


def test_issues_that_cannot_point_at_a_driver_leave_drivers_null():
    first = assumption("A-1", [driver()], value=500.0)
    second = assumption("A-2", [driver()], value=105.0)

    found = causes_of([first, second])

    assert found == {("value_out_of_range", "A-1"): None, ("not_distinct", "A-2"): None}


def test_one_assumption_can_carry_several_causes():
    twin = assumption("A-TWIN", [driver(), driver(name="event")], value=500.0)
    only_orders = [DataSource(kind="orders", item_scope="same_item")]

    assert causes_of([twin], only_orders) == {
        ("no_evidence", "A-TWIN"): ["category_trend", "event"],
        ("double_counted", "A-TWIN"): ["category_trend", "event"],
        ("value_out_of_range", "A-TWIN"): None,
    }
