# Optional orchestration

Use the current user's chosen model and provider. Model aliases depend on the selected backend; use explicit model IDs when a task requires a particular provider.

Delegate only independent work that benefits from separation. Keep one writer per file set and verify delegated output against the actual files and tests. A generated task or script does not grant permission for publishing, sending messages, changing credentials, installing packages or spending money.

Do not silently switch providers. Do not enable a paid priority tier or fast mode without the user's explicit choice. Logs used for diagnosis can contain private content; keep them local and share only redacted examples.

This optional policy applies when the user explicitly selects the orchestration launcher. It does not change the default permission mode or install third-party plugins.
