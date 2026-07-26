"""OPT-13/TG-06 回归测试：前后端 API 契约静态对齐。

历史已有 ≥4 例 FE/BE 漂移 bug（双层 .data 解包、persona 缓存路径、review 字段名、
legacy 列表遗漏 canonical），全部源于"对齐靠人眼"。本测试解析 app.js 的全部
api.get/post 调用路径（模板参数归一为 {param}），与 plugin_pages.py 注册表比对，
断言前端调用集合 ⊆ 后端注册集合。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
APP_JS = ROOT / "pages" / "admin" / "app.js"
PLUGIN_PAGES = ROOT / "astrmai" / "webui" / "plugin_pages.py"


def _normalize(path: str) -> str:
    # 前端模板参数 ${segment(x)} / ${x} 与后端 {name} 统一归一为 {p}
    path = re.sub(r"\$\{[^}]*\}", "{p}", path)
    path = re.sub(r"\{[^}]*\}", "{p}", path)
    # 去掉查询串
    return path.split("?", 1)[0].rstrip("/") or "/"


def _frontend_paths() -> set[str]:
    source = APP_JS.read_text(encoding="utf-8")
    paths: set[str] = set()
    # api.get("/x") / api.post(`/x/${id}`) 两种引号形态
    for match in re.finditer(r"api\.(?:get|post|put|delete)\(\s*([`\"'])(.*?)\1", source):
        raw = match.group(2)
        if raw.startswith("/"):
            paths.add(_normalize(raw))
    return paths


def _backend_paths() -> set[str]:
    source = PLUGIN_PAGES.read_text(encoding="utf-8")
    paths: set[str] = set()
    for match in re.finditer(r"\(\s*\"(GET|POST|PUT|DELETE)\"\s*,\s*\"(/[^\"]+)\"", source):
        paths.add(_normalize(match.group(2)))
    return paths


class FrontendBackendContractTests(unittest.TestCase):
    def test_every_frontend_call_has_backend_route(self):
        frontend = _frontend_paths()
        backend = _backend_paths()

        self.assertGreater(len(frontend), 30, "前端路径解析异常（数量过少，正则可能失配）")
        self.assertGreater(len(backend), 60, "后端注册表解析异常（数量过少，正则可能失配）")

        missing = sorted(frontend - backend)
        self.assertEqual(
            missing,
            [],
            f"前端调用了未注册的后端路径（FE/BE 漂移）：{missing}",
        )

    def test_normalization_examples(self):
        self.assertEqual(_normalize("/memories/canonical/${segment(id)}"), "/memories/canonical/{p}")
        self.assertEqual(_normalize("/memories/canonical/{memory_id}"), "/memories/canonical/{p}")
        self.assertEqual(_normalize("/reviews?page=1"), "/reviews")


if __name__ == "__main__":
    unittest.main()
