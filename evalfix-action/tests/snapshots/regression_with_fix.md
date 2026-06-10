<!-- evalfix-action -->
## EvalFix — 2 regressions  ❌ 0/4 passed (base: 2/3)

**What broke:** Both failures are expired-warranty refund cases. The agent treats the 30-day window as sufficient even when warranty coverage has ended.

**Root cause:** `prompt` · confidence 0.91

<details><summary>Failing cases (2)</summary>

| case | grader | why it failed |
|---|---|---|
| `refund_expired_warranty_direct` | semantic | Approved an expired-warranty refund. |
| `refund_expired_warranty_polite` | semantic | The answer approves a refund even though the warranty expired, contradicting the expected policy and exposing the busin… |

</details>

**New case, failing:** `new_refund_case`

### Proposed fix (not applied)
```diff
--- prompt.txt
+++ prompt.txt
@@ -1 +1 @@
- Refunds are available within 30 days of purchase.
+ Refunds are available within 30 days of purchase, only while the warranty is active.
```
**Evidence:** with this patch 40/40 pass · all 38 previously-passing cases intact ✅

---
Apply locally with `evalfix fix agents/support` · [docs](https://github.com/evalfix/evalfix-action#readme) · re-run by pushing a commit
