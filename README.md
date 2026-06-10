# EvalFix Action Sample

This repository demonstrates the local `evalfix-action` against a small support
agent.

## First run

1. Create a GitHub repository and push this directory to its default branch.
2. Add `ANTHROPIC_API_KEY` under **Settings → Secrets and variables → Actions**.
3. Create a branch.
4. In `agents/support/prompt.txt`, remove the requirement that the warranty must
   still be active.
5. Commit the change, push the branch, and open a pull request.

EvalFix should detect `refund_expired_warranty` as a regression, propose a fix
without applying it, update one sticky PR comment, and fail the workflow.

The workflow is in `.github/workflows/evalfix.yml`. The action is vendored in
`evalfix-action/`, so no marketplace publication is required.
