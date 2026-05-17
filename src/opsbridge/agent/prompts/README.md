# OpsBridge system prompt

The file `system.md` next to this README is the **default** system prompt
the agent uses at session start. It is loaded read-only from the
installed package (`importlib.resources`) — do not edit it at runtime.

## Per-host override

To customize per host, write a markdown file to:

```
/etc/opsbridge/agent/system_prompt.md
```

The override **must** contain these anchor strings verbatim, otherwise
it is rejected at session start and the default is used:

- `## Hard rules`
- `ask before destructive`
- `preferences file is special`
- `never fabricate tool output`
- `NOPASSWD sudo`

Validation is content-level (`anchor in text`), so you can reformat
surrounding prose freely.

Use `opsbridge doctor --system-prompt` to verify your override
parses, and to see the sha256 of the prompt that will be loaded.
