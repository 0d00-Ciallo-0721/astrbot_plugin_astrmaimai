# AstrMai Final Functional Audit

This directory stores the final production-code functional audit reports.

## Scope

- Review the current working tree, including uncommitted production changes.
- Review functional correctness, runtime stability, and module cooperation.
- Do not review tests, test coverage, security policy, code style, or refactoring opportunities.
- Do not modify production code.
- Treat `astrmai/infrastructure/security/` as an opaque framework-facing dependency.

## Reports

1. `01_entry_app_config.md`
2. `02_conversation_ingress_attention.md`
3. `03_conversation_decision_planning.md`
4. `04_conversation_execution_presentation.md`
5. `05_memory_retrieval_rag.md`
6. `06_memory_write_governance.md`
7. `07_gateway_context_economy.md`
8. `08_runtime_persistence_shared.md`
9. `09_state_sessions.md`
10. `10_learning_proactive.md`
11. `11_multimodal_workmode.md`
12. `12_webui_plugin_pages.md`

After all domain reports are complete, a separate integration review will produce
`13_cross_module_integration.md` and a consolidated final report.

## Finding Format

Each finding must include severity, file and line, trigger condition, real call
chain, actual behavior, expected behavior, production impact, existing guard,
classification, and confidence. Findings without a reachable production path
must not be reported as confirmed defects.
