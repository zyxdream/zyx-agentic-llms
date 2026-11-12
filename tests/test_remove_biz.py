
from backend.answer_drafter import remove_biz_type

def test_remove_works():
    ref = "Google, LLC"
    assert remove_biz_type(ref) == "Google"

    ref = "Waymo Inc."
    assert remove_biz_type(ref) == "Waymo"

