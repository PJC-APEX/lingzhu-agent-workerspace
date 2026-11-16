from app.utils.diff import compare_text, utf16_offset_to_index


def test_compare_text_marks_replaced_and_added_content():
    segments = compare_text("原来的文案。", "新的文案！")
    assert any(item["type"] in {"changed", "added"} for item in segments)
    assert "".join(item["text"] for item in segments) == "新的文案！"


def test_utf16_offsets_match_browser_coordinates():
    text = "甲😀乙"
    assert utf16_offset_to_index(text, 1) == 1
    assert utf16_offset_to_index(text, 3) == 2
    assert utf16_offset_to_index(text, 4) == 3
