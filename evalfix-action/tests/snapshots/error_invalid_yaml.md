<!-- evalfix-action -->
## EvalFix — setup needs attention ⚠️

### evals.yaml could not be parsed

Fix the YAML error below and push a new commit:

```text
while parsing a block mapping
expected <block end>, but found "-"
```

Minimal valid example:

```yaml
tests:
  - id: greeting
    input: "Hello"
    expected: "Respond politely"
    grader: semantic
```
