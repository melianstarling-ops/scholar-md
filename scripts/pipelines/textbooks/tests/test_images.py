import os
from PIL import Image
from scripts.pipelines.textbooks.images import is_visual_block, crop_filename, crop_block_images


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


def _make_test_png(path, size=(200, 200)):
    img = Image.new("RGB", size, color="white")
    for x in range(50, 150):
        for y in range(50, 150):
            img.putpixel((x, y), (255, 0, 0))   # 红色方块 [50,50,150,150]
    img.save(path)


def test_crop_block_images_saves_correct_region(tmp_path):
    png = str(tmp_path / "page_0001.png")
    _make_test_png(png)
    assets_dir = str(tmp_path / "out.assets")
    blocks = [{"block_label": "image", "block_id": 3,
               "block_bbox": [50, 50, 150, 150], "block_content": ""}]
    warnings = crop_block_images(png, blocks, assets_dir, page=1)
    assert warnings == []
    saved = Image.open(os.path.join(assets_dir, "page_0001_block_3.png"))
    assert saved.size == (100, 100)
    assert saved.getpixel((10, 10)) == (255, 0, 0)


def test_crop_block_images_skips_non_visual_labels(tmp_path):
    png = str(tmp_path / "page_0001.png")
    _make_test_png(png)
    assets_dir = str(tmp_path / "out.assets")
    blocks = [{"block_label": "text", "block_id": 1,
               "block_bbox": [0, 0, 10, 10], "block_content": "hi"}]
    warnings = crop_block_images(png, blocks, assets_dir, page=1)
    assert warnings == []
    assert not os.path.exists(assets_dir)


def test_crop_block_images_missing_bbox_warns_and_skips(tmp_path):
    png = str(tmp_path / "page_0001.png")
    _make_test_png(png)
    assets_dir = str(tmp_path / "out.assets")
    blocks = [{"block_label": "image", "block_id": 5,
               "block_bbox": None, "block_content": ""}]
    warnings = crop_block_images(png, blocks, assets_dir, page=1)
    assert len(warnings) == 1
    assert warnings[0]["kind"] == "visual_missing_bbox"
    assert warnings[0]["block_id"] == 5
    assert not os.path.exists(os.path.join(assets_dir, "page_0001_block_5.png")) \
        if os.path.isdir(assets_dir) else True


def test_crop_block_images_bad_bbox_warns_not_raises(tmp_path):
    png = str(tmp_path / "page_0001.png")
    _make_test_png(png)
    assets_dir = str(tmp_path / "out.assets")
    # x1<x0:Pillow crop 对反向坐标会抛异常
    blocks = [{"block_label": "image", "block_id": 9,
               "block_bbox": [150, 50, 50, 150], "block_content": ""}]
    warnings = crop_block_images(png, blocks, assets_dir, page=1)
    assert len(warnings) == 1
    assert warnings[0]["kind"] == "visual_crop_error"


def test_crop_block_images_skips_ordered_visual_block(tmp_path):
    # block_order 不为 None 的 image/chart 块超出 reconstruct.py 的处理范围
    # (只有 block_order is None 的可视块才会被渲染成图片链接),裁图函数应
    # 同步跳过,不裁不落文件,也不算错误(warnings 为空)。
    png = str(tmp_path / "page_0001.png")
    _make_test_png(png)
    assets_dir = str(tmp_path / "out.assets")
    blocks = [{"block_label": "image", "block_id": 1, "block_order": 1,
               "block_bbox": [50, 50, 150, 150], "block_content": ""}]
    warnings = crop_block_images(png, blocks, assets_dir, page=1)
    assert warnings == []
    assert not os.path.exists(assets_dir)
