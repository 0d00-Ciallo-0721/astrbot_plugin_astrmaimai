# 开发窗口 11：安全专项修复

## 必须先读取的审查报告
1. `artifacts/reviews/r11-security.md` — 完整安全审查（3 CRITICAL, 4 HIGH, 6 MEDIUM, 3 LOW）
2. `artifacts/reviews/r15-master.md` — 总报告

## 审查范围
全量代码（~120+ 源文件），覆盖：
- 认证/授权
- 注入风险（LLM prompt 注入、SQL 注入）
- 密钥/令牌管理
- 反序列化安全
- 路径穿越
- 加密实践

## 依赖
跨模块审查，可在任意阶段执行（建议在窗口 01-10 完成后做最终安全检查）

---

## 🔴 CRITICAL（3 项）

详见 `r11-security.md` 的 CRITICAL 部分。预计涵盖：
1. 认证中间件覆盖缺口（如某些 route 未加 auth 装饰器）
2. 硬编码凭证或密钥
3. 用户输入未校验直接传给敏感操作

---

## 🟡 HIGH（4 项）+ MEDIUM（6 项）

详见 `r11-security.md`：
- LLM prompt 注入风险（用户消息直接拼接到 system prompt）
- SQL 注入风险（字符串拼接 SQL 而非参数化查询）
- pickle/json 反序列化安全性
- 文件路径校验（路径穿越风险）
- 弱加密算法或明文存储

---

## 验证命令
```powershell
# 安全审查以手工验证为主
# 1. 搜索硬编码密钥
python -c "import os; [print(os.path.join(r,f)) for r,d,fs in os.walk('astrmai') for f in fs if f.endswith('.py') and any(k in open(os.path.join(r,f),errors='ignore').read().lower() for k in ['password=', 'secret=', 'token=', 'api_key=', 'private_key='])]"
# 2. 搜索不安全的反序列化
python -c "import os; [print(os.path.join(r,f)) for r,d,fs in os.walk('astrmai') for f in fs if f.endswith('.py') and 'pickle.load' in open(os.path.join(r,f),errors='ignore').read()]"
# 3. 搜索字符串拼接 SQL
python -c "import os; [print(os.path.join(r,f)) for r,d,fs in os.walk('astrmai') for f in fs if f.endswith('.py') and ('f\"' in open(os.path.join(r,f),errors='ignore').read() or \"f'\" in open(os.path.join(r,f),errors='ignore').read()) and 'sql' in open(os.path.join(r,f),errors='ignore').read().lower()]"
```

## 成功标准
- 🔴 3 CRITICAL 修复
- 全量安全扫描通过（无硬编码密钥、无 pickle.load 用户输入、无 SQL 字符串拼接）
