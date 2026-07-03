from scripts.pipelines.textbooks.images import is_visual_block, crop_filename


def test_is_visual_block_true_for_image_and_chart():
    assert is_visual_block("image") is True
    assert is_visual_block("chart") is True


def test_is_visual_block_false_for_others():
    assert is_visual_block("header_image") is False
    assert is_visual_block("table") is False
    assert is_visual_block("text") is False


def test_crop_filename_format():
    assert crop_filename(6, 3) == "page_0006_block_3.png"
    assert crop_filename(100, 0) == "page_0100_block_0.png"
