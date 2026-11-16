from zaar_assistant.planning import net_requirement, plan_material, plan_production, round_up_to_batch


def test_worked_example_370_rounds_to_400():
    # 500 demand + 100 safety - 180 on hand - 50 incoming = 370 -> 400 with 100-unit batches
    assert net_requirement(500, 100, 180, 50) == 370
    p = plan_production(500, 100, 180, 50, batch_size=100, capacity=600)
    assert (p.net_requirement, p.planned_production, p.capacity_exception) == (370, 400, False)


def test_net_requirement_never_negative():
    assert net_requirement(200, 40, 300, 0) == 0


def test_capacity_exception_is_flagged_not_silent():
    p = plan_production(800, 150, 300, 100, batch_size=100, capacity=550)
    assert p.net_requirement == 550 and p.batch_rounded == 600
    assert p.planned_production == 500          # capped at whole batches that fit
    assert p.capacity_exception and p.capacity_shortfall == 100


def test_round_up_to_batch():
    assert round_up_to_batch(0, 100) == 0
    assert round_up_to_batch(1, 100) == 100
    assert round_up_to_batch(400, 100) == 400


def test_material_reorder_respects_pack_size_and_moq():
    # need 1590 + safety 200 - on hand 900 - incoming 300 = 590 -> packs of 100 -> 600 (>= MOQ 500)
    m = plan_material(1590, 200, 900, 300, pack_size=100, moq=500)
    assert (m.gross_requirement, m.reorder_quantity) == (590, 600)
    # tiny gap still has to meet the MOQ
    assert plan_material(10, 0, 0, 0, pack_size=100, moq=500).reorder_quantity == 500
    # nothing needed -> order nothing (MOQ does not force an order)
    assert plan_material(0, 100, 400, 0, pack_size=100, moq=500).reorder_quantity == 0
