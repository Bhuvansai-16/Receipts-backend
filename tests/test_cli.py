from receipts.__main__ import load_patch


def test_patch_file_is_read_as_utf8(tmp_path):
    f = tmp_path / "fix.diff"
    f.write_bytes("+name = 'café'\r\n".encode("utf-8"))
    assert load_patch(str(f), gold="G") == ("+name = 'café'\n", "fix")


def test_patch_keywords():
    assert load_patch("gold", gold="G") == ("G", "gold")
    assert load_patch("none", gold="G") == (None, "none")
