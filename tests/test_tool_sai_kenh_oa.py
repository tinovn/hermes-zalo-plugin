"""Model gọi tool của kênh OA trong chat Zalo cá nhân thì được chỉ sang tool đúng.

Từ 10/09 plugin ``hermes-zalo-oa`` đăng ký ``oa_upload_recent_image_to_landing``
vào registry chung. Model bắt đầu gọi nó cả trong chat Zalo cá nhân, rơi vào
nhánh deny cuối ``_zalo_pre_tool_call_hook`` và nhận câu "tool này chỉ chạy cho
chủ tài khoản (sếp)" — model hiểu là hết quyền nên quay ra bảo khách "gửi lại
ảnh, đừng gửi dạng File". Đo trên vnnic-dn ngày 11/09: 71 lượt gọi nhầm, 49
phiên dính, chỉ 5 phiên bot tự quay về tool đúng.

``adapter.py`` import ``gateway.*`` (không có ngoài máy cài Hermes) nên bài test
nhấc hàm thuần ``goi_y_tool_dung_kenh`` cùng bảng tra ra bằng ``ast``, giống
cách ``test_owner_slash_command_gate.py`` làm.
"""

import ast
import os
import unittest
from typing import Any, Dict, Optional  # noqa: F401 — namespace cho hàm nhấc ra

_ADAPTER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "adapter.py"
)


def _load_goi_y():
    with open(_ADAPTER, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    can = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "_TOOL_OA_SANG_CA_NHAN" for t in node.targets
        ):
            can.append(node)
        if isinstance(node, ast.FunctionDef) and node.name == "goi_y_tool_dung_kenh":
            can.append(node)
    ns: Dict[str, Any] = {"Optional": Optional, "Dict": Dict}
    exec(compile(ast.Module(body=can, type_ignores=[]), _ADAPTER, "exec"), ns)
    return ns["goi_y_tool_dung_kenh"], ns["_TOOL_OA_SANG_CA_NHAN"]


class ToolSaiKenhOA(unittest.TestCase):
    def setUp(self):
        self.goi_y, self.bang = _load_goi_y()

    def test_tool_up_anh_oa_duoc_chi_sang_tool_ca_nhan(self):
        r = self.goi_y("oa_upload_recent_image_to_landing")
        self.assertIsNotNone(r, "phải chặn kèm gợi ý, không để rơi xuống deny suông")
        self.assertEqual(r["action"], "block")
        self.assertIn("zalo_upload_recent_image_to_landing", r["message"])

    def test_khong_bao_khach_gui_lai_anh(self):
        # Gốc thiệt hại: bot bảo khách gửi lại ảnh dù ảnh vẫn nằm trên máy chủ.
        self.assertIn("KHÔNG báo khách gửi lại ảnh",
                      self.goi_y("oa_upload_recent_image_to_landing")["message"])

    def test_tool_gui_file_va_anh_cung_duoc_chi_duong(self):
        self.assertIn("zalo_send_image", self.goi_y("oa_send_image")["message"])
        self.assertIn("zalo_send_file", self.goi_y("oa_send_file")["message"])

    def test_tool_khac_van_di_tiep_luong_deny_thuong(self):
        for ten in ("shell_exec", "zalo_send_image", "mcp_tino_landing_update", ""):
            self.assertIsNone(self.goi_y(ten), f"{ten!r} không được nhận gợi ý kênh OA")

    def test_ten_tool_hoa_thuong_va_thua_khoang_trang(self):
        self.assertIsNotNone(self.goi_y("  OA_Upload_Recent_Image_To_Landing  "))

    def test_bang_tra_chi_chua_tool_oa(self):
        for k, v in self.bang.items():
            self.assertTrue(k.startswith("oa_"), k)
            self.assertTrue(v.startswith("zalo_"), v)


if __name__ == "__main__":
    unittest.main()
