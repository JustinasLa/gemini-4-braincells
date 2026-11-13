<!-- entire-agent:begin -->
Read .entire/agent-guide.md for this repository's workflow, source inspection, and verification guidance.
@.entire/agent-guide.md
<!-- entire-agent:end -->

## Workflow with Entire

- One Trail per feature: from your feature branch run `entire trail create --title "<what and why>" --type feature`. Commits pushed to that branch, and the agent sessions behind them, attach to the Trail.
- Commit agent work with the agent running, so Entire links the commit to its session (`Entire-Checkpoint:` trailer). Don't amend or rebase away those trailers.
- Before merging, run `entire review general`. It reviews locally with Claude Code (no Entire credit; Codex-only users ask a teammate to run it) and posts the verdict to the Trail. Fix or dismiss each finding, then open the PR with `entire trail ci-pr`, undraft it with `entire trail ready`, and squash-merge it while keeping the checkpoint trailers in the squash commit:

  ```bash
  gh pr merge <n> --squash --body "$(git log --format='%(trailers:key=Entire-Checkpoint)' origin/main..HEAD | grep . | sort -u)"
  ```

  A squash without those trailers loses the link between the commit on `main` and its agent sessions.
- Don't run `entire trail rerun-review` or enable project Agents on entire.com: those run paid cloud agents. Ask the repo owner first.
- Never paste keys or `.env` contents into a prompt: transcripts are stored in checkpoints.
