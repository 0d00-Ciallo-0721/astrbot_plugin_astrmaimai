import json
import subprocess
import unittest
from pathlib import Path


class AdminGatewayHealthRenderTests(unittest.TestCase):
    def test_real_payload_rendering_covers_empty_unknown_escape_and_aggregate(self):
        root = Path(__file__).resolve().parents[1]
        app_js = root / "pages" / "admin" / "app.js"
        payload = {
            "model": {
                "task:provider/model-a": {
                    "pool_name": "task",
                    "model_id": "provider/model-a",
                    "status": "cooldown",
                    "consecutive_5xx": 2,
                    "skipped_requests": 3,
                    "cooldown_remaining_sec": 299.5,
                    "last_error_type": "server_error",
                },
                "vision:provider/<unsafe>": {
                    "pool_name": "vision",
                    "model_id": "provider/<unsafe>",
                    "status": "future_state",
                    "last_error_type": "<script>alert(1)</script>",
                },
            },
            "provider": {
                "provider": {
                    "health_scope": "provider_aggregate",
                    "read_only": True,
                    "cooldown_pool_count": 1,
                    "half_open_pool_count": 0,
                    "pool_count": 2,
                    "consecutive_5xx": 2,
                    "skipped_requests": 3,
                    "pools": {"task": {"status": "cooldown"}, "vision": {"status": "healthy"}},
                }
            },
            "provider_only": {
                "provider": {
                    "health_scope": "provider_aggregate",
                    "read_only": True,
                    "cooldown_pool_count": 0,
                    "half_open_pool_count": 0,
                    "pool_count": 0,
                    "consecutive_5xx": 0,
                    "skipped_requests": 0,
                    "provider_cooldown": {"status": "cooldown"},
                }
            },
        }
        node_script = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const sandbox = {
  console,
  window: { addEventListener() {} },
  document: { readyState: "loading", addEventListener() {} },
  location: { hash: "" },
  setInterval,
  clearInterval,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(`${source}\nthis.__renderGatewayHealth = renderGatewayHealth;`, sandbox);
const payload = JSON.parse(process.argv[2]);
const html = sandbox.__renderGatewayHealth(payload.model, payload.provider);
if (!html.includes("cooldown") || !html.includes("Provider 聚合")) throw new Error("cooldown/aggregate rendering missing");
if (!html.includes('chip muted">future_state</span>')) throw new Error("unknown state is not muted");
if (html.includes("<script>alert(1)</script>") || !html.includes("&lt;script&gt;alert(1)&lt;/script&gt;")) throw new Error("HTML escaping failed");
const empty = sandbox.__renderGatewayHealth({}, {});
if (!empty.includes("当前没有模型健康事件") || !empty.includes("当前没有 provider 健康事件")) throw new Error("empty rendering missing");
const providerOnly = sandbox.__renderGatewayHealth({}, payload.provider_only);
if (!providerOnly.includes('chip danger">cooldown</span>')) throw new Error("provider-only cooldown is not visible");
"""
        result = subprocess.run(
            ["node", "-e", node_script, str(app_js), json.dumps(payload, ensure_ascii=False)],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
