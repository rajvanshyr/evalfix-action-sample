# EvalFix Action

CI that catches your agent being wrong, not just crashing — and shows you the fix.

> **GIF placeholder:** EvalFix detecting a broken refund case, updating one sticky
> PR comment, and showing a verified prompt diff.

## Quickstart

The caller checks out the repository; EvalFix sets up Python 3.11 and installs the CLI.

```yaml
name: Agent evals
on: [pull_request]
permissions:
  contents: read
  pull-requests: write
jobs:
  evalfix:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: evalfix/evalfix-action@v0
        with: {project_dir: agents/support, suggest_fix: "true"}
        env: {ANTHROPIC_API_KEY: "${{ secrets.ANTHROPIC_API_KEY }}", GITHUB_TOKEN: "${{ github.token }}"}
```

Your project directory must contain:

```text
agents/support/
├── prompt.txt
└── evals.yaml
```

## No evals yet?

Install the CLI and generate a native `evals.yaml`:

```bash
python -m pip install evalfix
evalfix init agents/support
```

Commit the generated files before enabling the action.

## See it catch a regression

For the first PR, deliberately delete an important behavioral sentence from
`prompt.txt`, such as a refund eligibility constraint. Open the PR and EvalFix
will compare the edited prompt with the base branch, diagnose newly broken
cases, and post its proposed fix without changing your code.

## Inputs

| input | required | default | description |
|---|---:|---:|---|
| `project_dir` | yes | — | Directory containing `prompt.txt` and `evals.yaml`. |
| `suggest_fix` | no | `"false"` | Diagnose regressions and show a verified proposed diff. |
| `fail_on_regression` | no | `"true"` | Exit 1 only for base-pass/head-fail regressions. |
| `baseline` | no | `base-branch` | Use `base-branch` comparison or `none` for absolute results. |
| `max_cases` | no | `100` | Run only the first N cases and disclose truncation. |
| `timeout_minutes` | no | `15` | Per-CLI-invocation timeout. |
| `comment` | no | `"true"` | Disable for CI-only/local output mode. |

Outputs: `passed`, `total`, `regressions`, and `score_delta`.

`ANTHROPIC_API_KEY` is required. `GITHUB_TOKEN` is required when `comment` is
enabled. A one-line result is always appended to `$GITHUB_STEP_SUMMARY`.

## Fork pull requests

GitHub does not expose repository secrets to untrusted fork pull requests.
EvalFix detects that case, skips evaluation, writes a step-summary note, and
exits successfully. Do not switch the workflow to `pull_request_target`: doing
so could expose your LLM key to code controlled by the fork.

## Security

The action has no code-write behavior: head evaluation, base evaluation, and
fix generation all run in temporary copies. Proposed changes are displayed but
never applied or committed. Your code and keys never leave your runners through
an EvalFix service; the installed CLI communicates directly with the configured
LLM provider using `ANTHROPIC_API_KEY`.

Only grant:

```yaml
permissions:
  contents: read
  pull-requests: write
```

The action uses the GitHub REST API solely to create or update one comment whose
first line is `<!-- evalfix-action -->`.

## Local development

```bash
conda create -n evalfix-action python=3.11 -y
conda activate evalfix-action
python -m pip install -e ".[dev]"
make check
```

The manual smoke test calls the real CLI and therefore consumes model tokens:

```bash
python -m pip install evalfix
export ANTHROPIC_API_KEY=...
make smoke
```

It creates a temporary git repository, breaks the demo prompt, evaluates base
and head with the same five cases, runs the fix loop in another temporary copy,
prints the full regression comment, and expects exit code 1 from the action.

## License

MIT. See [LICENSE](LICENSE).
