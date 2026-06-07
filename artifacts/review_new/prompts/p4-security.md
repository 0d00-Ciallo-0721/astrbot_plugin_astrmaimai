# P4 — 安全加固（~1h）

> 基于终审报告未修复项 | 2 项 | 🟡 中等

---

## #1 `mock_frontend_server.py` — 默认密码环境变量化

**文件：** `astrmai/webui/mock_frontend_server.py:651,664`
**问题：** 默认密码 `"astrmai_admin"` 字面量暴露。虽仅绑定 `127.0.0.1` 且 `__debug__` 守卫已加，但最佳实践是强制环境变量。
**修复：**

```python
# 将
MOCK_AUTH_PASSWORD = os.environ.get("ASTRMAI_MOCK_WEBUI_PASSWORD", "astrmai_admin")
# 改为
MOCK_AUTH_PASSWORD = os.environ.get("ASTRMAI_MOCK_WEBUI_PASSWORD")
if not MOCK_AUTH_PASSWORD:
    raise RuntimeError("ASTRMAI_MOCK_WEBUI_PASSWORD must be set")
```

**验证：** 未设环境变量时启动应报错；设置后正常。

---

## #2 `url_validator.py` — DNS 重绑定 TOCTOU

**文件：** `astrmai/infrastructure/security/url_validator.py`
**问题：** `validate_image_url` 在 DNS 解析和 HTTP 请求之间使用 hostname 而非 IP。攻击者在两次 DNS 查询间切换 A 记录可绕过 IP 黑名单。
**修复：**

```python
# 解析 DNS 后缓存 IP 地址
resolved_ip = ...  # 当前已有 socket.getaddrinfo 逻辑

# 在 validate_image_url 返回值中携带 resolved_ip
# 调用方（executor.py、vision_binding.py）用 IP 直连而非 hostname
```

**注意：** 此修复需网络级出站 IP 规则作为纵深防御配合。单独修复 TOCTOU 能提高门槛但不能完全消除风险。

**验证：** `python -m pytest tests/test_security_url_validator_refactor.py -q`
