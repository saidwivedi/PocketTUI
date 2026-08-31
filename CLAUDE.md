# PocketTUI

## Releases
- Version is `0.8.<commit count>`, computed by `deploy_cloudflare.sh` from `git rev-list --count HEAD`.
- Every successful prod deploy auto-tags the deployed commit as `v0.8.N` and pushes the tag. Deploy from a clean, committed tree so the tag matches what shipped.
- "What is in prod" = the latest `v0.8.*` tag. Diff prod vs current work with `git diff <latest-tag>..main`.
- Run the test suite before pushing to main.
