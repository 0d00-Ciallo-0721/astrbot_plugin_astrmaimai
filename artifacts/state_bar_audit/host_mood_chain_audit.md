# Host Mood Chain Audit

- status: `passed`
- model: `openai/kimi-k2.5`
- all matched: `True`

## Cases
- `positive_short`
  - text: `谢谢你呀，我真的好开心，贴贴。`
  - direct: `happy` / `0.1000`
  - host: `happy` / `0.1000`
  - source: `attention_ingress`
  - matched: `True`
- `hostile_short`
  - text: `闭嘴，烦死了，你真讨厌。`
  - direct: `angry` / `-0.2000`
  - host: `angry` / `-0.2000`
  - source: `attention_ingress`
  - matched: `True`
- `mixed_short`
  - text: `谢谢你，但我还是有点难过。`
  - direct: `sad` / `-0.0600`
  - host: `sad` / `-0.0600`
  - source: `attention_ingress`
  - matched: `True`
- `tool_intent_short`
  - text: `帮我查一下明天上海天气。`
  - direct: `neutral` / `0.0000`
  - host: `neutral` / `0.0000`
  - source: `attention_ingress`
  - matched: `True`
- `sarcasm_short`
  - text: `你可真行啊，又把事情搞砸了，真棒。`
  - direct: `angry` / `-0.1800`
  - host: `angry` / `-0.1800`
  - source: `attention_ingress`
  - matched: `True`
