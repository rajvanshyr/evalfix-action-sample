<!-- evalfix-action -->
## EvalFix — setup needs attention ⚠️

### ANTHROPIC_API_KEY is not configured

EvalFix needs an Anthropic API key to run your agent evals.

Add these two lines to the workflow step that uses EvalFix:

```yaml
env:
  ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

Then add the repository secret in GitHub. [GitHub secrets docs](https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions)
